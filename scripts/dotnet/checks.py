#!/usr/bin/env python3
"""Sentinel Seed: Prüfungen der API im Build-Job (templates/steps/dotnet-checks.yml).

  todos     eine Warnung je TODO(onboarding) in project.yaml, mit Zeilennummer
  packages  NuGet-Pakete der Solution, nach dotnet restore:
            - Vorabversionen direkter Referenzen: Fehler, außer das Paket steht mit reason in
              build.prereleasePackages
            - bekannte Lücken, auch in transitiven Paketen: High und Critical sind Fehler, außer die
              Advisory steht mit reason in build.ignoreAdvisories; Low und Moderate sind Warnungen
            - veraltete direkte Referenzen: Warnung; Bfs.Seed.* meldet steps/seed-versions.yml
  format    dotnet format whitespace --verify-no-changes auf der Solution
  suppressions
            RS0030 (verbotene APIs) und die Regeln aus Bfs.Seed (SEEDAUTH…, SEEDSUPPRESS…,
            SEEDOPENAPI…) nur mit Begründung abgeschaltet, in .editorconfig, .globalconfig,
            Projektdateien, .props, .targets und .rsp (ohne bin, obj, node_modules). Begründung ist ein
            Kommentar in der Zeile davor mit mindestens 10 Zeichen; ohne ist es ein Fehler. Analyzer
            pauschal aus (RunAnalyzers false, dotnet_analyzer_diagnostic.severity) gibt eine Warnung.

Läuft im Checkout des Projekts. Werte aus dem Repo und Ausgaben von dotnet kommen nur bereinigt ins
Log. Scheitert dotnet list package am Netz oder an sich selbst, gibt es eine Warnung statt eines
Fehlers: Ein Ausfall von nuget.org soll weder PR noch Deploy aufhalten. Die Ausnahmen in
project.yaml prüft das Skript trotzdem; ein Eintrag ohne reason ist immer ein Fehler.

Braucht Python ab 3.10 und für packages PyYAML (Paket python3-yaml, auf Ubuntu vorinstalliert).

  python3 scripts/dotnet/checks.py todos --project-yaml project.yaml
  python3 scripts/dotnet/checks.py packages --solution api/Api.slnx --project-yaml project.yaml
  python3 scripts/dotnet/checks.py format --solution api/Api.slnx
  python3 scripts/dotnet/checks.py suppressions
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

# Lücken fragt die Prüfung immer bei nuget.org ab (GitHub Advisory Database), nicht bei den Quellen
# aus nuget.config: Ein privater Feed (Azure Artifacts) liefert keine Daten zu Lücken, und
# dotnet list package meldet dann still keine. Abgefragt wird nur die Liste der Lücken, kein Paket.
NUGET_ORG = "https://api.nuget.org/v3/index.json"
# Auch „TODO (onboarding)“ mit Leerraum vor der Klammer.
TODO_MARKER = re.compile(r"TODO\s*\(onboarding\)", re.IGNORECASE)
# Mindestlänge einer Begründung ohne Leerraum am Rand, in allen Seed-Repos gleich (Analyzer, Trivy).
MIN_REASON = 10
# Pakete von Seed selbst; zurückliegende Versionen meldet steps/seed-versions.yml, nicht diese Prüfung.
SEED_PACKAGE_PREFIX = "bfs.seed."
SEVERITY_ERROR = ("high", "critical")
SEVERITY_WARNING = ("low", "moderate")
# dotnet list package wiederholt Anfragen an einen Feed ohne Anmeldung (401) wie dotnet restore
# lange; ohne Grenze stünde der Job bis zu seinem Timeout.
LIST_TIMEOUT = 300
FORMAT_TIMEOUT = 600
# So viele Dateien nennt die Formatprüfung einzeln, danach nur noch die Zahl.
MAX_FILES = 20
CHECK_DOC = "README von seed-pipelines, „Prüfungen im Build“"


# --- Log ---------------------------------------------------------------------------------------

def clean(value: object) -> str:
    """Ohne Steuerzeichen und ##vso[: Meldungen enthalten Werte aus dem Repo (project.yaml,
    Paketnamen, Pfade), und der Agent führt Logging-Befehle in jeder Zeile aus, auch mitten im Text.
    U+0085, U+2028 und U+2029 zählen als Zeilenumbruch, falls etwas sie so liest."""
    return re.sub(r"[\x00-\x1f\x7f\x85  ]", " ", str(value)).replace("##vso[", "##vso_[")


def short(value: object, limit: int = 200) -> str:
    text = clean(value).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _property(value: object) -> str:
    """Wert einer Eigenschaft von task.logissue: ; und ] beenden sie sonst (Escaping des Agents)."""
    return clean(value).replace("%", "%AZP25").replace(";", "%3B").replace("]", "%5D")


class Log:
    """Gibt Meldungen sofort aus und zählt Fehler und Warnungen. In Azure Pipelines (TF_BUILD)
    werden Fehler und Warnungen zu Annotationen am Lauf, mit Datei und Zeile, wo bekannt."""

    def __init__(self) -> None:
        self.errors = 0
        self.warnings = 0

    def info(self, message: str) -> None:
        print(clean(message), flush=True)

    def error(self, message: str, path: str | None = None, line: int | None = None, column: int | None = None) -> None:
        self.errors += 1
        self._issue("error", message, path, line, column)

    def warning(self, message: str, path: str | None = None, line: int | None = None, column: int | None = None) -> None:
        self.warnings += 1
        self._issue("warning", message, path, line, column)

    @staticmethod
    def _issue(kind: str, message: str, path: str | None, line: int | None, column: int | None) -> None:
        if os.environ.get("TF_BUILD"):
            properties = [f"type={kind}"]
            if path:
                properties.append(f"sourcepath={_property(path)}")
                if line:
                    properties.append(f"linenumber={int(line)}")
                    if column:
                        properties.append(f"columnnumber={int(column)}")
            print(f"##vso[task.logissue {';'.join(properties)}]{clean(message)}", flush=True)
        else:
            where = ":".join(str(part) for part in (path, line, column) if part)
            prefix = "Fehler" if kind == "error" else "Warnung"
            print(f"{prefix}: {clean(where) + ': ' if where else ''}{clean(message)}", flush=True)


# --- dotnet ------------------------------------------------------------------------------------

@dataclass
class Result:
    code: int | None          # None: nicht gestartet oder nach dem Timeout beendet
    stdout: str
    stderr: str
    seconds: float = 0.0
    problem: str = ""         # warum code None ist

    def tail(self, lines: int = 4) -> str:
        """Die letzten Zeilen der Ausgabe als eine Zeile, für eine Meldung."""
        text = "\n".join(part for part in (self.stdout, self.stderr) if part.strip())
        rows = [row.strip() for row in text.splitlines() if row.strip()]
        return short(" | ".join(rows[-lines:]), 600) if rows else "keine Ausgabe"


Runner = Callable[[list, int], Result]


def run(args: list[str], timeout: int) -> Result:
    """Startet einen Befehl ohne Shell und fängt seine Ausgabe ab, damit nichts unbereinigt ins Log
    kommt. Nach dem Timeout endet die ganze Prozessgruppe, auch Knoten von MSBuild."""
    env = dict(os.environ, DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1", MSBUILDDISABLENODEREUSE="1")
    posix = os.name == "posix"
    started = time.monotonic()
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", env=env, start_new_session=posix)
    except OSError as error:
        return Result(None, "", "", 0.0, f"{args[0]} ließ sich nicht starten: {error}")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return Result(process.returncode, stdout, stderr, time.monotonic() - started)
    except subprocess.TimeoutExpired:
        if posix:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        stdout, stderr = process.communicate()
        return Result(None, stdout, stderr, time.monotonic() - started, f"nach {timeout} s abgebrochen")


def parse_json(text: str) -> dict | None:
    """Das JSON-Objekt aus der Ausgabe von dotnet list package; Hinweise des SDK davor stören nicht."""
    for match in re.finditer(r"^\{", text, re.MULTILINE):
        try:
            data, _ = json.JSONDecoder().raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("projects"), list):
            return data
    return None


@dataclass
class Package:
    id: str
    requested: str
    resolved: str
    latest: str
    project: str
    framework: str
    transitive: bool
    auto_referenced: bool
    vulnerabilities: list[tuple[str, str]]   # (Schwere, URL der Advisory)


def iter_packages(data: dict) -> Iterator[Package]:
    for project in data.get("projects") or []:
        if not isinstance(project, dict):
            continue
        # Kurzname wie im Build-Log (Api, Api.Tests); der Pfad wäre der des Agents.
        name = Path(str(project.get("path") or "?").replace("\\", "/")).stem
        for framework in project.get("frameworks") or []:
            if not isinstance(framework, dict):
                continue
            for key, transitive in (("topLevelPackages", False), ("transitivePackages", True)):
                for item in framework.get(key) or []:
                    if not isinstance(item, dict) or not item.get("id"):
                        continue
                    vulnerabilities = [
                        (str(v.get("severity") or ""), str(v.get("advisoryurl") or ""))
                        for v in item.get("vulnerabilities") or [] if isinstance(v, dict)
                    ]
                    yield Package(
                        id=str(item["id"]),
                        requested=str(item.get("requestedVersion") or ""),
                        resolved=str(item.get("resolvedVersion") or ""),
                        latest=str(item.get("latestVersion") or ""),
                        project=name,
                        framework=str(framework.get("framework") or ""),
                        transitive=transitive,
                        auto_referenced=bool(item.get("autoReferenced")),
                        vulnerabilities=vulnerabilities,
                    )


def is_prerelease(version: str) -> bool:
    """SemVer: ein Bindestrich vor den Build-Metadaten (+…), etwa 11.0.0-preview.1."""
    return "-" in version.split("+", 1)[0]


def joined(values: set[str] | list[str]) -> str:
    items = sorted(values, key=str.lower)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " und " + items[-1]


def projects(names: set[str]) -> str:
    return ("Projekt " if len(names) == 1 else "Projekte ") + joined(names)


# --- project.yaml ------------------------------------------------------------------------------

class ConfigError(Exception):
    pass


@dataclass
class Entry:
    key: str      # Paket-ID oder Advisory, wie in project.yaml
    reason: str
    line: int
    used: bool = False


@dataclass
class BuildConfig:
    path: str
    prerelease: dict[str, Entry] = field(default_factory=dict)       # Schlüssel klein geschrieben
    advisories: dict[str, Entry] = field(default_factory=dict)
    problems: list[tuple[str, int]] = field(default_factory=list)    # (Meldung, Zeile)


class _Mapping(dict):
    """dict mit der Zeile, in der die Abbildung in project.yaml beginnt."""
    line = 0


def load_yaml(text: str):
    """project.yaml mit PyYAML (safe), dazu Zeilennummern an jeder Abbildung für die Meldungen."""
    try:
        import yaml
    except ImportError:
        raise ConfigError("PyYAML fehlt. Auf eigenen Agents das Paket python3-yaml installieren "
                          "(README von seed-pipelines, „Eigene Agents“).") from None

    class Loader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        data = _Mapping()
        data.line = node.start_mark.line + 1
        yield data
        data.update(loader.construct_mapping(node))

    Loader.add_constructor("tag:yaml.org,2002:map", mapping)
    try:
        return yaml.load(text, Loader=Loader)  # noqa: S506 – Loader leitet von SafeLoader ab
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f" (Zeile {mark.line + 1})" if mark is not None else ""
        problem = getattr(error, "problem", None) or str(error)
        raise ConfigError(f"ist kein gültiges YAML{where}: {problem}") from None


def _entries(build: dict, key: str, id_key: str, config: BuildConfig) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    raw = build.get(key)
    if raw is None:
        return entries
    if not isinstance(raw, list):
        config.problems.append((f"build.{key} muss eine Liste sein, je Eintrag {id_key} und reason.", build.line))
        return entries
    for item in raw:
        line = getattr(item, "line", 0) or build.line
        if not isinstance(item, dict):
            config.problems.append((f"build.{key}: Eintrag „{short(item, 80)}“ ist kein Objekt; erwartet {id_key} und reason.", line))
            continue
        name, reason = item.get(id_key), item.get("reason")
        if not isinstance(name, str) or not name.strip():
            config.problems.append((f"build.{key}: Eintrag ohne {id_key}.", line))
            continue
        if reason is None or (isinstance(reason, str) and not reason.strip()):
            config.problems.append((f"build.{key}: {short(name, 120)} ohne reason. Jede Ausnahme braucht eine "
                                    "Begründung, die im PR zu sehen ist.", line))
            continue
        if not isinstance(reason, str):
            # yes, no, Zahlen oder Daten liest YAML nicht als Text.
            config.problems.append((f"build.{key}: reason von {short(name, 120)} ist kein Text; in Anführungszeichen setzen.", line))
            continue
        if len(reason.strip()) < MIN_REASON:
            config.problems.append((f"build.{key}: reason von {short(name, 120)} ist zu kurz („{short(reason.strip(), 40)}“); "
                                    f"mindestens {MIN_REASON} Zeichen, die den Grund nennen.", line))
            continue
        entries[name.strip().lower()] = Entry(name.strip(), reason.strip(), line)
    return entries


def load_build_config(path: str) -> BuildConfig:
    """Liest build.prereleasePackages und build.ignoreAdvisories. Fehlerhafte Einträge landen in
    problems; eine unlesbare Datei ist ein ConfigError."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise ConfigError(f"nicht lesbar: {error.strerror or error}") from None
    data = load_yaml(text)
    config = BuildConfig(path)
    if not isinstance(data, dict):
        raise ConfigError("ist leer oder keine Abbildung mit Schlüsseln.")
    build = data.get("build")
    if build is None:
        return config
    if not isinstance(build, dict):
        config.problems.append(("build muss eine Abbildung sein, etwa mit prereleasePackages und ignoreAdvisories.",
                                getattr(data, "line", 0) or 1))
        return config
    config.prerelease = _entries(build, "prereleasePackages", "name", config)
    config.advisories = _entries(build, "ignoreAdvisories", "id", config)
    return config


# --- Prüfungen ---------------------------------------------------------------------------------

def check_todos(path: str, log: Log) -> None:
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        log.error(f"{path} nicht lesbar: {error.strerror or error}")
        return
    found = 0
    for number, line in enumerate(lines, start=1):
        if TODO_MARKER.search(line):
            found += 1
            log.warning(f"Offenes TODO(onboarding) in {path}, Zeile {number}: {short(line, 160)}", path, number)
    if found:
        log.info(f"{found} offene TODO(onboarding) in {path}. Den Punkt erledigen und die Markierung entfernen.")
    else:
        log.info(f"Keine offenen TODO(onboarding) in {path}.")


def list_packages(dotnet: str, solution: str, options: list[str], label: str, runner: Runner, log: Log) -> dict | None:
    """dotnet list package mit --format json; bei Netz- oder Werkzeugfehlern eine Warnung und None."""
    # Ab .NET 10 stellt dotnet list package vorher selbst wieder her. Geprüft wird aber, was der Schritt
    # dotnet restore aufgelöst hat; ein zweiter Restore kostet nur Zeit und Netz.
    args = [dotnet, "list", solution, "package", *options, "--format", "json", "--no-restore"]
    result = runner(args, LIST_TIMEOUT)
    if result.code not in (0, None) and "--no-restore" in result.stdout + result.stderr:
        # Ein SDK vor .NET 10 kennt den Schalter nicht, stellt aber auch nicht selbst wieder her.
        result = runner(args[:-1], LIST_TIMEOUT)
    command = " ".join(["dotnet list package", *options])
    data = parse_json(result.stdout) if result.code == 0 else None
    if data is None:
        reason = result.problem or (f"Code {result.code}" if result.code else "keine JSON-Ausgabe")
        log.warning(f"{label} nicht möglich ({command}: {reason}): {result.tail()}. Der Build läuft weiter; "
                    "meist ist eine Paketquelle nicht erreichbar oder verlangt eine Anmeldung (preBuildSteps).")
        return None
    log.info(f"{command}: {result.seconds:.0f} s")
    for problem in data.get("problems") or []:
        if isinstance(problem, dict) and problem.get("text"):
            log.warning(f"{command}: {short(problem.get('text'), 300)}")
    return data


def check_prerelease(data: dict, config: BuildConfig, log: Log) -> None:
    found: dict[str, dict] = {}
    for package in iter_packages(data):
        # Nur direkte Referenzen. Implizite Pakete des SDK (autoReferenced) folgen global.json.
        if package.transitive or package.auto_referenced or not is_prerelease(package.resolved):
            continue
        group = found.setdefault(package.id.lower(), {"id": package.id, "versions": set(), "projects": set()})
        group["versions"].add(package.resolved)
        group["projects"].add(package.project)
    for key, group in sorted(found.items()):
        what = f"{group['id']} {joined(group['versions'])} ({joined(group['projects'])})"
        entry = config.prerelease.get(key)
        if entry:
            entry.used = True
            log.info(f"Vorabversion erlaubt: {what}. Grund laut {config.path}: {short(entry.reason, 300)}")
        else:
            log.error(f"Vorabversion ohne Begründung: {what}. Eine stabile Version referenzieren oder das Paket mit "
                      f"reason in {config.path} unter build.prereleasePackages eintragen ({CHECK_DOC}).")
    if not found:
        log.info("Keine Vorabversionen unter den direkten Paketreferenzen.")
    for entry in config.prerelease.values():
        if not entry.used:
            log.warning(f"build.prereleasePackages nennt {short(entry.key, 120)}, aber keine direkte Referenz nutzt davon eine "
                        "Vorabversion. Den Eintrag entfernen.", config.path, entry.line)


def advisory_id(url: str) -> str:
    """GHSA-… aus https://github.com/advisories/GHSA-…; sonst die URL selbst."""
    return url.rstrip("/").rsplit("/", 1)[-1] if "/" in url else url


def check_vulnerabilities(data: dict, config: BuildConfig, log: Log) -> None:
    found: dict[str, dict] = {}
    for package in iter_packages(data):
        for severity, url in package.vulnerabilities:
            ident = advisory_id(url) or "?"
            group = found.setdefault(ident.lower(), {"id": ident, "url": url, "severity": severity,
                                                     "packages": set(), "projects": set()})
            group["packages"].add(f"{package.id} {package.resolved}")
            group["projects"].add(f"{package.project} (transitiv)" if package.transitive else package.project)
    for key, group in sorted(found.items()):
        severity = group["severity"]
        what = (f"Lücke {group['id']} ({severity or 'Schwere unbekannt'}) in {joined(group['packages'])}, "
                f"{projects(group['projects'])}: {group['url']}")
        entry = config.advisories.get(key) or config.advisories.get(group["url"].lower())
        if entry:
            entry.used = True
            log.info(f"{what}. Hingenommen laut {config.path}: {short(entry.reason, 300)}")
        elif severity.lower() in SEVERITY_ERROR:
            log.error(f"{what}. Das Paket auf eine Version ohne die Lücke heben (ein transitives Paket mit einer direkten "
                      f"Referenz) oder die Advisory mit reason in {config.path} unter build.ignoreAdvisories eintragen "
                      f"({CHECK_DOC}).")
        else:
            # Low, Moderate und unbekannte Schwere: sichtbar, aber kein Halt.
            log.warning(f"{what}. Das Paket bei Gelegenheit heben.")
    if not found:
        log.info("Keine bekannten Lücken in den Paketen (direkt und transitiv).")


def check_outdated(data: dict, log: Log) -> None:
    found: dict[str, dict] = {}
    seed: set[str] = set()
    for package in iter_packages(data):
        if package.transitive or package.auto_referenced or not package.latest or package.latest == package.resolved:
            continue
        if package.id.lower().startswith(SEED_PACKAGE_PREFIX) and package.resolved:
            # Meldet schon der Schritt „Seed-Versionen prüfen“ (steps/seed-versions.yml), samt Hinweis
            # auf das passende Template; hier käme es doppelt. Ohne aufgelöste Version sieht auch jener
            # Schritt nichts; dann meldet diese Prüfung das Paket wie jedes andere.
            seed.add(package.id)
            continue
        group = found.setdefault(package.id.lower(), {"id": package.id, "versions": set(), "latest": package.latest,
                                                      "projects": set()})
        group["versions"].add(package.resolved or package.requested or "?")
        group["projects"].add(package.project)
    for _, group in sorted(found.items()):
        if not re.match(r"[0-9]", group["latest"]):
            # Etwa „Not found at the sources“: kein Feed kennt das Paket (mehr).
            log.info(f"{group['id']}: keine Version in den Paketquellen gefunden ({short(group['latest'], 80)}).")
            continue
        log.warning(f"Veraltet: {group['id']} {joined(group['versions'])}, neueste Version {group['latest']} "
                    f"({projects(group['projects'])}).")
    if seed:
        log.info(f"Nicht als veraltet gemeldet, das übernimmt „Seed-Versionen prüfen“: {joined(seed)}.")
    if not found and not seed:
        log.info("Alle direkten Paketreferenzen sind aktuell.")


def check_packages(dotnet: str, solution: str, project_yaml: str, runner: Runner, log: Log) -> None:
    try:
        config = load_build_config(project_yaml)
    except ConfigError as error:
        log.error(f"{project_yaml} {error}", project_yaml)
        return
    for message, line in config.problems:
        log.error(message, project_yaml, line)

    plain = list_packages(dotnet, solution, [], "Prüfung auf Vorabversionen", runner, log)
    if plain is not None:
        check_prerelease(plain, config, log)

    vulnerable = list_packages(dotnet, solution, ["--vulnerable", "--include-transitive", "--source", NUGET_ORG],
                               "Prüfung auf bekannte Lücken", runner, log)
    if vulnerable is not None:
        check_vulnerabilities(vulnerable, config, log)
        # Nur wenn die Prüfung lief: Sonst wäre jeder Eintrag „unbenutzt“.
        for entry in config.advisories.values():
            if not entry.used:
                log.warning(f"build.ignoreAdvisories nennt {short(entry.key, 120)}, die Prüfung findet diese Lücke nicht "
                            "mehr. Den Eintrag entfernen.", project_yaml, entry.line)

    outdated = list_packages(dotnet, solution, ["--outdated"], "Prüfung auf veraltete Pakete", runner, log)
    if outdated is not None:
        check_outdated(outdated, log)


def read_format_report(path: Path, root: Path) -> list[tuple[str, int, int, int]]:
    """(Datei relativ zum Checkout, Anzahl Stellen, erste Zeile, erste Spalte) je Datei."""
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    # Eine Datei kann mehrfach im Bericht stehen, etwa je Formatierer (Leerraum, Zeilenende) oder je
    # Projekt, das sie einbindet; gezählt wird sie einmal, jede Stelle auch.
    places: dict[str, set[tuple[int, int, str]]] = {}
    for document in report if isinstance(report, list) else []:
        if not isinstance(document, dict):
            continue
        changes = [c for c in document.get("FileChanges") or [] if isinstance(c, dict)]
        if not changes:
            continue
        file_path = str(document.get("FilePath") or document.get("FileName") or "?")
        try:
            relative = os.path.relpath(file_path, root)
            file_path = file_path if relative.startswith("..") else relative
        except ValueError:
            pass   # anderes Laufwerk (nur Windows)
        places.setdefault(file_path.replace("\\", "/"), set()).update(
            (int(c.get("LineNumber") or 0), int(c.get("CharNumber") or 0), str(c.get("FormatDescription") or ""))
            for c in changes)
    files = []
    for file_path, found in places.items():
        line, column, _ = min(found)
        files.append((file_path, len(found), line, column))
    return sorted(files)


def check_format(dotnet: str, solution: str, runner: Runner, log: Log, root: Path) -> None:
    fix = f"dotnet format whitespace {solution}"
    with tempfile.TemporaryDirectory(prefix="seed-format-") as directory:
        result = runner([dotnet, "format", "whitespace", solution, "--verify-no-changes", "--report", directory],
                        FORMAT_TIMEOUT)
        files = read_format_report(Path(directory, "format-report.json"), root)
    if result.code == 0 and not files:
        log.info(f"Formatierung ohne Abweichungen ({result.seconds:.0f} s).")
        return
    if result.code in (0, 2) and files:
        for file_path, count, line, column in files[:MAX_FILES]:
            places = "einer Stelle" if count == 1 else f"{count} Stellen"
            log.error(f"Formatierung: {file_path} weicht an {places} ab, zuerst Zeile {line}.", file_path, line, column)
        more = f" (die ersten {MAX_FILES} oben)" if len(files) > MAX_FILES else ""
        log.error(f"dotnet format whitespace: {len(files)} Datei(en) weichen von der Formatierung laut .editorconfig "
                  f"ab{more}. Lokal beheben mit „{fix}“ im Checkout und das Ergebnis committen; nur prüfen mit "
                  f"„{fix} --verify-no-changes“.")
        return
    if result.code == 2:
        log.error(f"dotnet format whitespace meldet Abweichungen, aber keinen Bericht: {result.tail()}. Lokal beheben "
                  f"mit „{fix}“.")
        return
    reason = result.problem or f"Code {result.code}"
    log.warning(f"Formatierung nicht geprüft (dotnet format whitespace: {reason}): {result.tail()}")


# --- Abgeschaltete Regeln ----------------------------------------------------------------------
# Regeln, die ein Projekt nicht still abschalten soll: verbotene APIs (RS0030, BannedApiAnalyzers) und
# die Regeln aus Bfs.Seed. SEEDAUTH… und SEEDSUPPRESS… lassen sich ohnehin nicht konfigurieren; eine
# Zeile, die es versucht, gehört trotzdem begründet oder entfernt.
# Nur Codes mit Nummer: MSBuild-Eigenschaften wie SeedOpenApiEnabled oder SeedOpenApiReadme sind keine.
PROTECTED = re.compile(r"\b(RS0030|SEED(?:AUTH|SUPPRESS|OPENAPI)[0-9]+)\b", re.IGNORECASE)
# Schwere, mit der eine Regel nicht mehr als Warnung und damit nicht mehr als Fehler im Build ankommt.
WEAK_SEVERITIES = ("none", "silent", "hidden", "suggestion", "info")
SKIP_DIRECTORIES = ("bin", "obj", "node_modules", ".git")
# MSBuild-Eigenschaften, die Warnungen abschalten oder zu Meldungen bzw. bloßen Warnungen machen.
SUPPRESSING = ("nowarn", "warningsnotaserrors", "msbuildwarningsasmessages", "msbuildwarningsnotaserrors")
HARMLESS = ("warningsaserrors", "msbuildwarningsaserrors")


@dataclass
class Suppression:
    path: str
    line: int
    kind: str       # rule: Regel abgeschaltet, unclear: Regel erwähnt, bulk: Analyzer pauschal aus, generated
    what: str
    reason: str


def _comment_above(lines: list[str], index: int, markers: str) -> str:
    """Kommentarzeilen (etwa # oder ;) direkt über lines[index], als ein Text."""
    texts = []
    i = index - 1
    while i >= 0 and lines[i].strip() and lines[i].strip()[0] in markers:
        texts.insert(0, lines[i].strip()[1:].strip())
        i -= 1
    return " ".join(text for text in texts if text)


def _xml_comment_above(lines: list[str], index: int) -> str:
    """XML-Kommentar, der in der Zeile vor lines[index] endet, auch über mehrere Zeilen."""
    i = index - 1
    if i < 0 or not lines[i].rstrip().endswith("-->"):
        return ""
    collected = [lines[i]]
    while "<!--" not in collected[0] and i > 0:
        i -= 1
        collected.insert(0, lines[i])
    text = " ".join(part.strip() for part in collected)
    start = text.rfind("<!--")
    return " ".join(text[start + 4:-3].split()) if start >= 0 else ""


def _config_suppressions(path: str, text: str) -> list[Suppression]:
    """.editorconfig und .globalconfig: Schwere einzelner Regeln und pauschale Einstellungen."""
    found = []
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line[0] in "#;[" or "=" not in line:
            continue
        key, value = (part.strip().lower() for part in line.split("=", 1))
        value = (value.split() or [""])[0]
        match = re.fullmatch(r"dotnet_diagnostic\.([^.]+)\.severity", key)
        if match and PROTECTED.fullmatch(match.group(1)) and value in WEAK_SEVERITIES:
            kind, what = "rule", f"{match.group(1).upper()}, severity = {value}"
        elif re.fullmatch(r"dotnet_analyzer_diagnostic(\.category-[^.]+)?\.severity", key) and value in WEAK_SEVERITIES:
            kind, what = "bulk", f"{key} = {value}"
        elif key == "generated_code" and value == "true":
            kind, what = "generated", "generated_code = true"
        else:
            continue
        found.append(Suppression(path, index + 1, kind, what, _comment_above(lines, index, "#;")))
    return found


def _response_suppressions(path: str, text: str) -> list[Suppression]:
    """Antwortdateien (Directory.Build.rsp): MSBuild liest sie bei jedem Aufruf mit."""
    found = []
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules = sorted({m.group(1).upper() for m in PROTECTED.finditer(line)})
        if rules:
            found.append(Suppression(path, index + 1, "rule", f"{', '.join(rules)} in „{short(line, 80)}“",
                                     _comment_above(lines, index, "#")))
        elif re.search(r"RunAnalyzers(DuringBuild)?\s*=\s*false", line, re.IGNORECASE):
            found.append(Suppression(path, index + 1, "bulk", f"„{short(line, 80)}“", _comment_above(lines, index, "#")))
    return found


def _msbuild_suppressions(path: str, text: str) -> list[Suppression]:
    """Projektdateien, .props und .targets: Regeln in NoWarn und Co., Analyzer pauschal aus."""
    lines = text.splitlines()
    # Kommentare zählen nicht als Abschaltung; Zeilenumbrüche bleiben für die Zeilennummern.
    code = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.DOTALL)

    def line_of(offset: int) -> int:
        return code.count("\n", 0, offset) + 1

    found = []
    by_element: dict[int, tuple[str, set[str]]] = {}
    for match in PROTECTED.finditer(code):
        start = max(code.rfind("<", 0, match.start()), 0)
        context = code[start:match.start()]
        tag = re.match(r"<\s*/?\s*([\w.:-]+)", context)
        name = tag.group(1) if tag else "?"
        if ">" not in context:
            # Im Tag selbst, also in einem Attribut wie NoWarn="…"
            attribute = re.search(r"([\w.:-]+)\s*=\s*[\"'][^\"']*$", context)
            name = attribute.group(1) if attribute else name
        if name.lower() in HARMLESS:
            continue
        by_element.setdefault(start, (name, set()))[1].add(match.group(1).upper())
    for start, (name, rules) in sorted(by_element.items()):
        line = line_of(start)
        kind = "rule" if name.lower() in SUPPRESSING else "unclear"
        found.append(Suppression(path, line, kind, f"{', '.join(sorted(rules))} in <{name}>",
                                 _xml_comment_above(lines, line - 1)))
    bulk = (
        (r"<(RunAnalyzers|RunAnalyzersDuringBuild)\b[^>]*>\s*false\s*<", None),
        (r"<Analyzer\s[^>]*\bRemove\s*=", "<Analyzer Remove=…>"),
        (r"\bExcludeAssets\s*=\s*\"[^\"]*analyzers|<ExcludeAssets>[^<]*analyzers", "ExcludeAssets mit analyzers"),
    )
    for pattern, label in bulk:
        for match in re.finditer(pattern, code, re.IGNORECASE):
            line = line_of(match.start())
            what = label or f"<{match.group(1)}>false</{match.group(1)}>"
            found.append(Suppression(path, line, "bulk", what, _xml_comment_above(lines, line - 1)))
    return found


def find_suppressions(root: Path) -> list[Suppression]:
    found: list[Suppression] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d.lower() not in SKIP_DIRECTORIES)
        for name in sorted(files):
            lower = name.lower()
            if lower == ".editorconfig" or lower.endswith(".globalconfig"):
                scan = _config_suppressions
            elif lower.endswith((".csproj", ".props", ".targets")):
                scan = _msbuild_suppressions
            elif lower.endswith(".rsp"):
                scan = _response_suppressions
            else:
                continue
            full = Path(directory, name)
            try:
                text = full.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            found.extend(scan(full.relative_to(root).as_posix(), text))
    return found


def check_suppressions(root: Path, log: Log) -> None:
    found = find_suppressions(root)
    for item in found:
        where = f"{item.path}, Zeile {item.line}"
        justified = len(item.reason) >= MIN_REASON
        if item.kind in ("rule", "unclear", "generated") and justified:
            log.info(f"Ausnahme: {where}: {item.what}. Grund: {short(item.reason, 300)}")
        elif item.kind == "rule":
            why = f"die Begründung „{short(item.reason, 40)}“ ist zu kurz" if item.reason else "ohne Begründung"
            log.error(f"{where}: {item.what} schaltet eine geschützte Regel ab, {why}. In der Zeile davor einen "
                      f"Kommentar mit dem Grund ergänzen (mindestens {MIN_REASON} Zeichen) oder die Abschaltung "
                      f"entfernen ({CHECK_DOC}).", item.path, item.line)
        elif item.kind == "unclear":
            log.warning(f"{where}: {item.what}. Schaltet das die Regel ab, gehört in die Zeile davor ein Kommentar "
                        "mit dem Grund.", item.path, item.line)
        elif item.kind == "generated":
            log.warning(f"{where}: generated_code = true; für diese Dateien laufen keine Analyzer und keine "
                        "Formatprüfung. In der Zeile davor den Grund nennen.", item.path, item.line)
        else:
            because = f" Grund: {short(item.reason, 300)}" if item.reason else " Ohne Begründung."
            log.warning(f"{where}: {item.what} schaltet Analyzer pauschal ab, auch RS0030 und die Regeln aus "
                        f"Bfs.Seed.{because}", item.path, item.line)
    if not any(item.kind in ("rule", "unclear", "bulk") for item in found):
        log.info("Keine Abschaltung von RS0030, SEEDAUTH…, SEEDSUPPRESS… oder SEEDOPENAPI… gefunden.")


# --- Aufruf ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None, runner: Runner = run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    todos = commands.add_parser("todos", help="TODO(onboarding) in project.yaml")
    todos.add_argument("--project-yaml", default="project.yaml")
    packages = commands.add_parser("packages", help="Vorabversionen, Lücken und veraltete Pakete")
    packages.add_argument("--solution", required=True)
    packages.add_argument("--project-yaml", default="project.yaml")
    packages.add_argument("--dotnet", default="dotnet")
    fmt = commands.add_parser("format", help="dotnet format whitespace --verify-no-changes")
    fmt.add_argument("--solution", required=True)
    fmt.add_argument("--dotnet", default="dotnet")
    suppressions = commands.add_parser("suppressions", help="abgeschaltete RS0030- und Seed-Regeln")
    suppressions.add_argument("--root", default=".")
    args = parser.parse_args(argv)

    # UTF-8 auch unter LANG=C oder in einer Windows-Konsole (lokaler Aufruf).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log = Log()
    if args.command == "todos":
        check_todos(args.project_yaml, log)
    elif args.command == "packages":
        check_packages(args.dotnet, args.solution, args.project_yaml, runner, log)
    elif args.command == "suppressions":
        check_suppressions(Path(args.root).resolve(), log)
    else:
        check_format(args.dotnet, args.solution, runner, log, Path.cwd())
    if log.errors or log.warnings:
        log.info(f"{log.errors} Fehler, {log.warnings} {'Warnung' if log.warnings == 1 else 'Warnungen'}.")
    return 1 if log.errors else 0


if __name__ == "__main__":
    sys.exit(main())
