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

Läuft im Checkout des Projekts. Werte aus dem Repo und Ausgaben von dotnet kommen nur bereinigt ins
Log. Scheitert dotnet list package am Netz oder an sich selbst, gibt es eine Warnung statt eines
Fehlers: Ein Ausfall von nuget.org soll weder PR noch Deploy aufhalten. Die Ausnahmen in
project.yaml prüft das Skript trotzdem; ein Eintrag ohne reason ist immer ein Fehler.

Braucht Python ab 3.10 und für packages PyYAML (Paket python3-yaml, auf Ubuntu vorinstalliert).

  python3 scripts/dotnet/checks.py todos --project-yaml project.yaml
  python3 scripts/dotnet/checks.py packages --solution api/Api.slnx --project-yaml project.yaml
  python3 scripts/dotnet/checks.py format --solution api/Api.slnx
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
TODO_MARKER = re.compile(r"TODO\(onboarding\)", re.IGNORECASE)
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
    args = [dotnet, "list", solution, "package", *options, "--format", "json"]
    result = runner(args, LIST_TIMEOUT)
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
        if package.id.lower().startswith(SEED_PACKAGE_PREFIX):
            # Meldet schon der Schritt „Seed-Versionen prüfen“ (steps/seed-versions.yml), samt Hinweis
            # auf das passende Template; hier käme es doppelt.
            seed.add(package.id)
            continue
        group = found.setdefault(package.id.lower(), {"id": package.id, "versions": set(), "latest": package.latest,
                                                      "projects": set()})
        group["versions"].add(package.resolved)
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
    files = []
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
        first = min(changes, key=lambda c: (int(c.get("LineNumber") or 0), int(c.get("CharNumber") or 0)))
        files.append((file_path.replace("\\", "/"), len(changes), int(first.get("LineNumber") or 0),
                      int(first.get("CharNumber") or 0)))
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
    args = parser.parse_args(argv)

    # UTF-8 auch unter LANG=C oder in einer Windows-Konsole (lokaler Aufruf).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    log = Log()
    if args.command == "todos":
        check_todos(args.project_yaml, log)
    elif args.command == "packages":
        check_packages(args.dotnet, args.solution, args.project_yaml, runner, log)
    else:
        check_format(args.dotnet, args.solution, runner, log, Path.cwd())
    if log.errors or log.warnings:
        log.info(f"{log.errors} Fehler, {log.warnings} {'Warnung' if log.warnings == 1 else 'Warnungen'}.")
    return 1 if log.errors else 0


if __name__ == "__main__":
    sys.exit(main())
