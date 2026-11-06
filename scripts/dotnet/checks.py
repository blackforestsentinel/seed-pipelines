#!/usr/bin/env python3
"""Sentinel Seed: Prüfungen im Build-Job (templates/steps/dotnet-checks.yml und npm-checks.yml).

  todos     eine Warnung je TODO(onboarding) in project.yaml, mit Zeilennummer
  packages  NuGet-Pakete der Solution, nach dotnet restore:
            - Vorabversionen direkter Referenzen: Fehler, außer das Paket steht mit reason in
              build.prereleasePackages
            - bekannte Lücken, auch in transitiven Paketen: High und Critical sind Fehler, außer die
              Advisory steht mit reason in build.ignoreAdvisories; Low und Moderate sind Warnungen
            - veraltete direkte Referenzen: Warnung, wenn es in der genutzten Hauptversion etwas Neueres
              gibt; eine neue Hauptversion nur als Zeile im Log; Bfs.Seed.* meldet
              steps/seed-versions.yml
  npm       npm-Projekte im Repo, je package-lock.json (ohne node_modules), ohne npm ci:
            - bekannte Lücken der Laufzeit-Abhängigkeiten (npm audit --omit=dev
              --package-lock-only), auch transitiv: wie bei NuGet, dieselbe Liste build.ignoreAdvisories
            - Vorabversionen direkter Laufzeit-Abhängigkeiten: wie bei NuGet, dieselbe Liste
              build.prereleasePackages
  format    dotnet format whitespace --verify-no-changes auf der Solution
  suppressions
            RS0030 (verbotene APIs) und die Regeln aus Bfs.Seed (SEEDAUTH…, SEEDSUPPRESS…,
            SEEDOPENAPI…) nur mit Begründung abgeschaltet, in .editorconfig, .globalconfig,
            Projektdateien, .props, .targets und .rsp (ohne bin, obj, node_modules). Begründung ist ein
            Kommentar in der Zeile davor mit mindestens 10 Zeichen; ohne ist es ein Fehler. Analyzer
            pauschal aus (RunAnalyzers false, dotnet_analyzer_diagnostic.severity) gibt eine Warnung.

Läuft im Checkout des Projekts. Werte aus dem Repo und Ausgaben von dotnet und npm kommen nur
bereinigt ins Log. Scheitert dotnet list package oder npm audit am Netz oder an sich selbst, gibt es
eine Warnung statt eines Fehlers: Ein Ausfall von nuget.org oder der npm-Registry soll weder PR noch
Deploy aufhalten. Die Ausnahmen in project.yaml prüft das Skript trotzdem; ein Eintrag ohne reason
ist immer ein Fehler.

Ob ein Eintrag in build.prereleasePackages oder build.ignoreAdvisories nichts mehr trifft, entscheidet
über beide Prüfungen hinweg: packages mit --state-file legt sein Ergebnis in der Datei ab und
urteilt nicht selbst, npm mit derselben Datei urteilt für beide. Ohne die Datei urteilt jede
Prüfung nur für sich.

Braucht Python ab 3.10, für packages und npm PyYAML (Paket python3-yaml, auf Ubuntu vorinstalliert)
und für npm ein npm ab Version 7.

  python3 scripts/dotnet/checks.py todos --project-yaml project.yaml
  python3 scripts/dotnet/checks.py packages --solution api/Api.slnx --project-yaml project.yaml --state-file /tmp/seed.json
  python3 scripts/dotnet/checks.py npm --project-yaml project.yaml --state-file /tmp/seed.json
  python3 scripts/dotnet/checks.py format --solution api/Api.slnx
  python3 scripts/dotnet/checks.py suppressions
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
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
# npm audit braucht je Projekt wenige Sekunden. Antwortet die Registry nicht, wiederholt npm die
# Anfrage; ohne Grenze stünde der Job minutenlang.
NPM_TIMEOUT = 120
NPM_DEFAULT_REGISTRY = "registry.npmjs.org"
# Lock-Dateien, die npm audit liest; die Shrinkwrap-Datei hat bei npm Vorrang.
NPM_LOCK_FILES = ("npm-shrinkwrap.json", "package-lock.json")
# Lock-Dateien anderer Paketmanager: npm audit liest sie nicht.
OTHER_LOCK_FILES = ("yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb")
# So viele Advisories nennt eine Meldung je npm-Paket, die übrigen stehen im Log.
MAX_ADVISORIES = 8


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


# (Befehl, Timeout) für dotnet, dazu das Arbeitsverzeichnis für npm
Runner = Callable[..., Result]


def run(args: list[str], timeout: int, cwd: str | None = None) -> Result:
    """Startet einen Befehl ohne Shell und fängt seine Ausgabe ab, damit nichts unbereinigt ins Log
    kommt. Nach dem Timeout endet die ganze Prozessgruppe, auch Knoten von MSBuild."""
    # npm fragt sonst bei jedem Aufruf nach einer neueren eigenen Version.
    env = dict(os.environ, DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1", MSBUILDDISABLENODEREUSE="1",
               npm_config_update_notifier="false", npm_config_fund="false")
    posix = os.name == "posix"
    started = time.monotonic()
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
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


# --- Unbenutzte Ausnahmen über NuGet und npm hinweg --------------------------------------------
# build.prereleasePackages und build.ignoreAdvisories gelten für NuGet und npm. Ob ein Eintrag nichts
# mehr trifft, weiß erst, wer beide Ergebnisse kennt. Im Build-Job sind es zwei Schritte: „NuGet-Pakete
# prüfen“ vor dotnet build, „npm-Pakete prüfen“ nach der Einrichtung von Node. Der erste legt sein
# Ergebnis in einer Datei ab (--state-file, im Temp-Ordner des Jobs), der zweite urteilt für beide.

LISTS = {"prerelease": "build.prereleasePackages", "advisories": "build.ignoreAdvisories"}


@dataclass
class Usage:
    ran: bool                  # Prüfung vollständig gelaufen; sonst ist kein Eintrag sicher unbenutzt
    used: set[str] = field(default_factory=set)   # Schlüssel der getroffenen Einträge, klein geschrieben


def used_keys(entries: dict[str, Entry]) -> set[str]:
    return {key for key, entry in entries.items() if entry.used}


def save_state(path: str, check: str, usage: dict[str, Usage]) -> None:
    """Überschreibt die Datei: Sie gilt nur für den laufenden Job."""
    data = {check: {name: {"ran": item.ran, "used": sorted(item.used)} for name, item in usage.items()}}
    Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_state(path: str, check: str) -> dict[str, Usage] | None:
    """Ergebnis einer anderen Prüfung aus der Datei; None, wenn es fehlt oder nicht lesbar ist."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")).get(check)
        return {name: Usage(bool(data[name]["ran"]), {str(key) for key in data[name]["used"]}) for name in LISTS}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None


def warn_unused(config: BuildConfig, path: str, usage: dict[str, Usage], scope: str, log: Log) -> None:
    """Eine Warnung je Eintrag, den keine Prüfung getroffen hat; nur für Listen, deren Prüfungen alle
    liefen. Sonst wäre jeder Eintrag „unbenutzt“, etwa wenn die Registry nicht erreichbar war."""
    for name, entries in (("prerelease", config.prerelease), ("advisories", config.advisories)):
        unused = [entry for key, entry in entries.items() if key not in usage[name].used]
        if not unused:
            continue
        if not usage[name].ran:
            log.info(f"Ob alle Einträge in {LISTS[name]} noch nötig sind, bleibt offen: Die Prüfung ({scope}) lief "
                     "nicht vollständig.")
            continue
        for entry in unused:
            if name == "prerelease":
                log.warning(f"build.prereleasePackages nennt {short(entry.key, 120)}, aber keine direkte Referenz ({scope}) "
                            "nutzt davon eine Vorabversion. Den Eintrag entfernen.", path, entry.line)
            else:
                log.warning(f"build.ignoreAdvisories nennt {short(entry.key, 120)}, die Prüfung ({scope}) findet diese "
                            "Lücke nicht mehr. Den Eintrag entfernen.", path, entry.line)


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


def version_key(version: str) -> tuple | None:
    """Zahlen der Version zum Vergleichen; eine Vorabversion liegt vor der fertigen. None ohne Zahl vorn."""
    core, _, prerelease = version.split("+", 1)[0].partition("-")
    numbers = re.findall(r"[0-9]+", core)
    if not numbers or not re.match(r"[0-9]", core):
        return None
    return tuple(int(n) for n in numbers) + (0,) * (4 - len(numbers)), 0 if prerelease else 1


def is_newer(candidate: str, current: str) -> bool:
    new, old = version_key(candidate), version_key(current)
    return new is not None and old is not None and new > old


def new_major(package: Package) -> bool:
    """Liegt die neueste Version in einer höheren Hauptversion als die genutzte?"""
    new, old = version_key(package.latest), version_key(package.resolved)
    return new is not None and old is not None and new[0][0] > old[0][0]


def outdated_candidates(data: dict) -> tuple[list[Package], set[str]]:
    """Direkte Referenzen mit neuerer Version; Bfs.Seed.* mit aufgelöster Version extra."""
    packages, seed = [], set()
    for package in iter_packages(data):
        if package.transitive or package.auto_referenced or not package.latest or package.latest == package.resolved:
            continue
        if package.id.lower().startswith(SEED_PACKAGE_PREFIX) and package.resolved:
            # Meldet schon der Schritt „Seed-Versionen prüfen“ (steps/seed-versions.yml), samt Hinweis
            # auf das passende Template; hier käme es doppelt. Ohne aufgelöste Version sieht auch jener
            # Schritt nichts; dann meldet diese Prüfung das Paket wie jedes andere.
            seed.add(package.id)
            continue
        packages.append(package)
    return packages, seed


def check_outdated(data: dict, minor: dict | None, log: Log) -> None:
    """Warnung nur, wenn es in der genutzten Hauptversion etwas Neueres gibt. Eine neue Hauptversion
    nennt das Log ohne Annotation: Sie lässt sich oft nicht nehmen (etwa weil ein anderes Paket nur die
    alte unterstützt), und eine Warnung in jedem Lauf, die man nicht beheben kann, lehrt nur, Warnungen
    zu übersehen. data stammt aus --outdated (neueste Version überhaupt), minor aus --outdated
    --highest-minor (neueste derselben Hauptversion); minor fehlt, wenn keine Hauptversion neu ist."""
    packages, seed = outdated_candidates(data)
    within = {}
    for package in iter_packages(minor or {}):
        within[(package.id.lower(), package.project, package.framework)] = package.latest
    updates: dict[str, dict] = {}
    majors: dict[str, dict] = {}
    for package in packages:
        version = package.resolved or package.requested or "?"
        if not re.match(r"[0-9]", package.latest):
            # Etwa „Not found at the sources“: kein Feed kennt das Paket (mehr).
            log.info(f"{package.id}: keine Version in den Paketquellen gefunden ({short(package.latest, 80)}).")
            continue
        if new_major(package):
            group = majors.setdefault(package.id.lower(), {"id": package.id, "versions": set(), "latest": package.latest,
                                                           "projects": set()})
            group["versions"].add(version)
            group["projects"].add(package.project)
            same_major = within.get((package.id.lower(), package.project, package.framework), "")
        else:
            same_major = package.latest
        if same_major and (is_newer(same_major, package.resolved) or not package.resolved):
            group = updates.setdefault(package.id.lower(), {"id": package.id, "versions": set(), "latest": same_major,
                                                            "projects": set()})
            group["versions"].add(version)
            group["projects"].add(package.project)
    for _, group in sorted(updates.items()):
        log.warning(f"Veraltet: {group['id']} {joined(group['versions'])}, neueste Version derselben Hauptversion "
                    f"{group['latest']} ({projects(group['projects'])}).")
    for _, group in sorted(majors.items()):
        log.info(f"Neue Hauptversion verfügbar: {group['id']} {joined(group['versions'])} → {group['latest']} "
                 f"({projects(group['projects'])}).")
    if seed:
        log.info(f"Nicht als veraltet gemeldet, das übernimmt „Seed-Versionen prüfen“: {joined(seed)}.")
    if not updates:
        log.info("Alle direkten Paketreferenzen sind in ihrer Hauptversion aktuell.")


def check_packages(dotnet: str, solution: str, project_yaml: str, runner: Runner, log: Log,
                   state_file: str | None = None) -> None:
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

    outdated = list_packages(dotnet, solution, ["--outdated"], "Prüfung auf veraltete Pakete", runner, log)
    if outdated is not None:
        # Die zweite Abfrage nur, wenn ein Paket eine neue Hauptversion hat: Dann sagt --outdated nicht, ob
        # es in der genutzten Hauptversion etwas Neueres gibt. Sonst ist die neueste Version schon eine
        # derselben Hauptversion, und es bleibt bei einer Abfrage.
        minor = None
        if any(new_major(package) for package in outdated_candidates(outdated)[0]):
            minor = list_packages(dotnet, solution, ["--outdated", "--highest-minor"],
                                  "Prüfung auf neuere Versionen derselben Hauptversion", runner, log)
        check_outdated(outdated, minor, log)

    # Nur wenn eine Abfrage lief, zählt ein Eintrag ihrer Liste als „unbenutzt“.
    usage = {"prerelease": Usage(plain is not None, used_keys(config.prerelease)),
             "advisories": Usage(vulnerable is not None, used_keys(config.advisories))}
    if state_file:
        save_state(state_file, "nuget", usage)
        if config.prerelease or config.advisories:
            log.info("Ob Einträge in build.prereleasePackages und build.ignoreAdvisories nichts mehr treffen, entscheidet "
                     "„npm-Pakete prüfen“ für NuGet und npm zusammen.")
    else:
        warn_unused(config, project_yaml, usage, "NuGet", log)


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


# --- npm ---------------------------------------------------------------------------------------
# Geprüft wird jede Lock-Datei im Repo, nicht nur Frontend und Erweiterung: auch npm-Projekte, die
# ein Projekt in extraBuildSteps baut. npm audit liest mit --package-lock-only nur die Lock-Datei und
# braucht weder npm ci noch node_modules; so läuft die Prüfung vor jedem npm ci, also bevor
# Install-Skripte eines Pakets mit bekannter Lücke laufen.
# .terraform: Module, die terraform init lädt, gehören nicht zum Projekt.
NPM_SKIP_DIRECTORIES = ("bin", "obj", "node_modules", ".git", ".terraform")
# Nur Laufzeit-Abhängigkeiten: Lücken in Werkzeugen des Builds (Vite, Vitest, jsdom …) betreffen die
# ausgelieferte App nicht und wären der größte Teil der Meldungen. --no-offline: Mit offline=true in
# einer .npmrc fragte npm audit gar nicht und meldete still keine Lücken.
NPM_AUDIT = ["audit", "--json", "--omit=dev", "--package-lock-only", "--no-offline"]


@dataclass
class NpmProject:
    directory: Path      # Ordner mit der Lock-Datei
    label: str           # Ordner relativ zum Checkout, für Meldungen
    lock_path: str       # Lock-Datei relativ zum Checkout, für Annotationen
    lock: dict | None    # None: nicht lesbar
    problem: str = ""


@dataclass
class NpmUnchecked:
    label: str           # Ordner mit package.json, aber ohne npm-Lock-Datei
    other_lock: str      # yarn.lock und Co., sonst leer


def _relative(path: Path, root: Path) -> str:
    text = path.relative_to(root).as_posix()
    return "." if text in ("", ".") else text


def _label(directory: Path, root: Path) -> str:
    """Ordner eines npm-Projekts für Meldungen."""
    text = _relative(directory, root)
    return "Wurzelordner" if text == "." else text


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("kein JSON-Objekt")
    return data


def _has_runtime_dependencies(manifest: Path) -> bool:
    try:
        data = _read_json(manifest)
    except (OSError, ValueError):
        return True   # im Zweifel melden
    return any(isinstance(data.get(section), dict) and data[section]
               for section in ("dependencies", "optionalDependencies"))


def find_npm_projects(root: Path) -> tuple[list[NpmProject], list[NpmUnchecked]]:
    """Ordner mit npm-Lock-Datei, dazu Ordner mit package.json und Laufzeit-Abhängigkeiten, die keine
    Lock-Datei abdeckt. Mitglieder eines Workspaces deckt die Lock-Datei des Workspaces ab."""
    root = root.resolve()
    projects: list[NpmProject] = []
    manifests: dict[str, tuple[Path, str]] = {}   # package.json ohne npm-Lock-Datei: Ordner, andere Lock-Datei
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(d for d in subdirectories if d.lower() not in NPM_SKIP_DIRECTORIES)
        here = Path(directory)
        lock_name = next((name for name in NPM_LOCK_FILES if name in files), None)
        if lock_name:
            path = here / lock_name
            project = NpmProject(here, _label(here, root), _relative(path, root), None)
            try:
                project.lock = _read_json(path)
            except (OSError, ValueError) as error:
                project.problem = short(getattr(error, "strerror", None) or error, 200)
            projects.append(project)
        elif "package.json" in files:
            manifests[os.path.normcase(directory)] = (here, next((name for name in OTHER_LOCK_FILES if name in files), ""))

    covered = set()
    for project in projects:
        covered.add(os.path.normcase(project.directory))
        packages = (project.lock or {}).get("packages")
        for key in packages if isinstance(packages, dict) else ():
            # Workspaces stehen mit ihrem Ordner in packages, installierte Pakete unter node_modules.
            if key and "node_modules" not in key.split("/"):
                covered.add(os.path.normcase(os.path.normpath(project.directory / key)))
    unchecked = [NpmUnchecked(_relative(here, root), other)
                 for key, (here, other) in sorted(manifests.items())
                 if key not in covered and _has_runtime_dependencies(here / "package.json")]
    return projects, unchecked


def lock_version(lock: dict, node: str) -> str:
    """Version eines Pakets an seinem Ort in der Lock-Datei, etwa node_modules/axios."""
    packages = lock.get("packages")
    if isinstance(packages, dict):
        entry = packages.get(node)
        return str(entry.get("version") or "") if isinstance(entry, dict) else ""
    # lockfileVersion 1 (npm 6): verschachtelte dependencies statt packages
    entry: object = lock
    for name in (part.strip("/") for part in node.split("node_modules/")):
        if name:
            entry = ((entry.get("dependencies") if isinstance(entry, dict) else None) or {}).get(name)
    return str(entry.get("version") or "") if isinstance(entry, dict) and entry is not lock else ""


def npm_direct_dependencies(project: NpmProject) -> Iterator[tuple[str, dict]]:
    """(Name, Eintrag in der Lock-Datei) der direkten Laufzeit-Abhängigkeiten, auch der Workspaces."""
    lock = project.lock or {}
    packages = lock.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            if "node_modules" in key.split("/") or not isinstance(entry, dict):
                continue
            for section in ("dependencies", "optionalDependencies"):
                for name in entry.get(section) or {}:
                    # Auflösung wie bei Node: node_modules des Workspaces, dann die Ordner darüber.
                    parts = key.split("/") if key else []
                    for depth in range(len(parts), -1, -1):
                        found = packages.get("/".join([*parts[:depth], "node_modules", name]))
                        if isinstance(found, dict):
                            yield name, found
                            break
        return
    # lockfileVersion 1: direkte Abhängigkeiten nur aus package.json
    try:
        manifest = _read_json(project.directory / "package.json")
    except (OSError, ValueError):
        return
    installed = lock.get("dependencies") if isinstance(lock.get("dependencies"), dict) else {}
    for section in ("dependencies", "optionalDependencies"):
        names = manifest.get(section) if isinstance(manifest.get(section), dict) else {}
        for name in names:
            if isinstance(installed.get(name), dict):
                yield name, installed[name]


def check_npm_prerelease(projects: list[NpmProject], config: BuildConfig, path: str, log: Log) -> None:
    found: dict[str, dict] = {}
    for project in projects:
        for name, entry in npm_direct_dependencies(project):
            version = str(entry.get("version") or "")
            resolved = str(entry.get("resolved") or "")
            # Nur Pakete aus einer Registry: Workspaces (link), Ordner (file:) und Git-Repos tragen die
            # Version aus ihrer eigenen package.json, die keine Vorabversion im Sinne der Regel ist.
            if entry.get("link") or resolved.startswith(("git", "file:")) or not is_prerelease(version):
                continue
            group = found.setdefault(name.lower(), {"id": name, "versions": set(), "projects": set(), "locks": set()})
            group["versions"].add(version)
            group["projects"].add(project.label)
            group["locks"].add(project.lock_path)
    for key, group in sorted(found.items()):
        what = f"npm-Paket {group['id']} {joined(group['versions'])} ({joined(group['projects'])})"
        entry = config.prerelease.get(key)
        if entry:
            entry.used = True
            log.info(f"Vorabversion erlaubt: {what}. Grund laut {path}: {short(entry.reason, 300)}")
        else:
            lock = next(iter(group["locks"])) if len(group["locks"]) == 1 else None
            log.error(f"Vorabversion ohne Begründung: {what}. Eine stabile Version in package.json eintragen oder das "
                      f"Paket mit reason in {path} unter build.prereleasePackages eintragen ({CHECK_DOC}).", lock)
    if projects and not found:
        log.info("Keine Vorabversionen unter den direkten Laufzeit-Abhängigkeiten (npm).")


def redact(text: str) -> str:
    """Anmeldedaten in einer URL (https://user:token@host/) aus Meldungen von npm entfernen."""
    return re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1***@", text)


def parse_npm_json(text: str) -> dict | None:
    """Das erste JSON-Objekt in der Ausgabe von npm --json."""
    for match in re.finditer(r"^\s*\{", text, re.MULTILINE):
        try:
            data, _ = json.JSONDecoder().raw_decode(text, match.end() - 1)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def audit_failure(data: dict | None, result: Result) -> str:
    """Warum npm audit keinen Bericht lieferte, für die Warnung. Den Body der Antwort lässt die
    Meldung weg: Er kommt von der Registry oder einem Proxy und kann alles enthalten."""
    if data:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        parts = [str(part) for part in (data.get("message"), error.get("code"), error.get("summary")) if part]
        if data.get("statusCode"):
            parts.append(f"HTTP {data['statusCode']}")
        if parts:
            return short(redact(", ".join(parts)), 400)
        if "advisories" in data or "actions" in data:
            return "Ausgabe von npm vor Version 7, die Prüfung braucht npm ab Version 7"
    if result.problem:
        return result.problem
    return f"Code {result.code}: {short(redact(result.tail()), 400)}"


def npm_registry(npm: str, project: NpmProject, runner: Runner) -> str:
    """Registry, die npm audit in diesem Ordner fragt (.npmrc), ohne Anmeldedaten; leer, wenn unbekannt."""
    result = runner([npm, "config", "get", "registry"], 30, str(project.directory))
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()] if result.code == 0 else []
    if not lines:
        return ""
    try:
        parts = urllib.parse.urlsplit(lines[-1])
        host = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    except ValueError:
        return ""
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", "")) if host else ""


@dataclass
class NpmAdvisory:
    id: str
    url: str
    severity: str
    title: str


@dataclass
class NpmFinding:
    """Ein npm-Paket mit Lücken, über alle Projekte."""
    name: str
    versions: set[str] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)
    locks: set[str] = field(default_factory=set)
    advisories: dict[str, NpmAdvisory] = field(default_factory=dict)   # Schlüssel: Kennung klein


def collect_npm_vulnerabilities(report: dict, project: NpmProject, found: dict[str, NpmFinding]) -> None:
    for name, item in sorted(report["vulnerabilities"].items()):
        if not isinstance(item, dict):
            continue
        # via nennt Advisories als Objekt und verwundbare Abhängigkeiten als Namen. Ein Paket nur mit
        # Namen ist allein über eine Abhängigkeit betroffen; die meldet ihr eigener Eintrag.
        advisories = [via for via in item.get("via") or [] if isinstance(via, dict)]
        if not advisories:
            continue
        finding = found.setdefault(name, NpmFinding(name))
        versions = {lock_version(project.lock or {}, str(node)) for node in item.get("nodes") or []} - {""}
        finding.versions |= versions or {"?"}
        finding.projects.add(project.label if item.get("isDirect") else f"{project.label} (transitiv)")
        finding.locks.add(project.lock_path)
        for via in advisories:
            url = str(via.get("url") or "")
            ident = advisory_id(url) if url else f"npm-Advisory {via.get('source') or '?'}"
            finding.advisories.setdefault(ident.lower(), NpmAdvisory(ident, url, str(via.get("severity") or ""),
                                                                     str(via.get("title") or "")))


def _advisory_list(advisories: list[NpmAdvisory]) -> str:
    shown = [f"{a.id} ({a.severity or 'Schwere unbekannt'})" for a in advisories[:MAX_ADVISORIES]]
    more = len(advisories) - len(shown)
    return ", ".join(shown) + (f" und {more} weitere (siehe Log)" if more else "")


def _gaps(count: int) -> str:
    return "1 Lücke" if count == 1 else f"{count} Lücken"


def check_npm_vulnerabilities(found: dict[str, NpmFinding], config: BuildConfig, path: str, log: Log) -> None:
    """Eine Meldung je Paket statt je Advisory: Ein altes Paket hat bei npm oft ein Dutzend, und
    behoben werden sie alle mit derselben neuen Version. Die Ausnahmen gelten je Advisory."""
    for name, finding in sorted(found.items()):
        what = f"npm-Paket {name} {joined(finding.versions)} in {joined(finding.projects)}"
        open_advisories = []
        for key, advisory in sorted(finding.advisories.items()):
            entry = config.advisories.get(key) or config.advisories.get(advisory.url.lower())
            if entry:
                entry.used = True
                log.info(f"Lücke {advisory.id} ({advisory.severity or 'Schwere unbekannt'}) in {what}: {advisory.url}. "
                         f"Hingenommen laut {path}: {short(entry.reason, 300)}")
            else:
                open_advisories.append(advisory)
        if not open_advisories:
            continue
        severe = [a for a in open_advisories if a.severity.lower() in SEVERITY_ERROR]
        other = [a for a in open_advisories if a not in severe]
        lock = next(iter(finding.locks)) if len(finding.locks) == 1 else None
        if severe:
            also = f"; dazu {_gaps(len(other))} Low oder Moderate" if other else ""
            log.error(f"{what}: {_gaps(len(severe))} High oder Critical: {_advisory_list(severe)}{also}. Das Paket auf "
                      "eine Version ohne diese Lücken heben (etwa npm audit fix im Ordner des Projekts; ein transitives "
                      "Paket über die direkte Abhängigkeit oder overrides in package.json) oder jede Advisory mit reason "
                      f"in {path} unter build.ignoreAdvisories eintragen ({CHECK_DOC}).", lock)
        else:
            # Low, Moderate und unbekannte Schwere: sichtbar, aber kein Halt, wie bei NuGet.
            log.warning(f"{what}: {_gaps(len(other))} Low oder Moderate: {_advisory_list(other)}. Das Paket bei "
                        "Gelegenheit heben.", lock)
        for advisory in severe + other:
            log.info(f"  {advisory.id} ({advisory.severity or 'Schwere unbekannt'}) {name}: {short(advisory.title, 160)} "
                     f"{advisory.url}")


def audit_npm(projects: list[NpmProject], npm: str | None, config: BuildConfig, path: str, runner: Runner,
              log: Log) -> bool:
    """npm audit je Projekt; True, wenn alle Projekte geprüft sind."""
    if not projects:
        return True
    if npm is None:
        log.warning(f"npm-Pakete nicht auf Lücken geprüft: kein npm auf dem Agent ({joined({p.lock_path for p in projects})}). "
                    f"Node einrichten, etwa mit UseNode@1 in preBuildSteps ({CHECK_DOC}).")
        return False
    complete = True
    found: dict[str, NpmFinding] = {}
    for project in projects:
        registry = npm_registry(npm, project, runner)
        if registry and urllib.parse.urlsplit(registry).hostname != NPM_DEFAULT_REGISTRY:
            log.info(f"{project.label}: npm audit fragt {registry} (npm-Konfiguration, etwa .npmrc). Diese Registry "
                     "muss Daten zu Lücken liefern, wie Azure Artifacts über npmjs; sonst meldet npm still keine.")
        result = runner([npm, *NPM_AUDIT], NPM_TIMEOUT, str(project.directory))
        data = parse_npm_json(result.stdout)
        # Der Exit-Code sagt nichts: 1 auch bei Lücken, 0 trotz Lücken mit audit-level in .npmrc.
        if data is None or "auditReportVersion" not in data or not isinstance(data.get("vulnerabilities"), dict):
            complete = False
            log.warning(f"Prüfung auf bekannte Lücken in {project.label} nicht möglich (npm audit: "
                        f"{audit_failure(data, result)}). Der Build läuft weiter; meist ist die Registry nicht "
                        "erreichbar oder verlangt eine Anmeldung (preBuildSteps).", project.lock_path)
            continue
        counts = (data.get("metadata") or {}).get("dependencies") or {}
        prod = f"{counts['prod']} Laufzeit-Pakete, " if isinstance(counts, dict) and "prod" in counts else ""
        log.info(f"npm audit in {project.label}: {prod}{result.seconds:.0f} s")
        collect_npm_vulnerabilities(data, project, found)
    check_npm_vulnerabilities(found, config, path, log)
    if complete and not found:
        log.info("Keine bekannten Lücken in den Laufzeit-Abhängigkeiten (npm, direkt und transitiv).")
    return complete


def check_npm(root: Path, project_yaml: str, npm: str | None, runner: Runner, log: Log,
              state_file: str | None = None) -> None:
    try:
        config = load_build_config(project_yaml)
    except ConfigError as error:
        log.error(f"{project_yaml} {error}", project_yaml)
        return
    for message, line in config.problems:
        log.error(message, project_yaml, line)

    projects, unchecked = find_npm_projects(root)
    for item in unchecked:
        other = f" ({item.other_lock}; die Prüfung liest nur package-lock.json)" if item.other_lock else ""
        manifest = "package.json" if item.label == "." else f"{item.label}/package.json"
        log.warning(f"{manifest} hat Laufzeit-Abhängigkeiten, aber keine package-lock.json{other}. Lücken und "
                    "Vorabversionen sind dort nicht prüfbar. Die Lock-Datei mit npm install erzeugen und einchecken.",
                    manifest)
    readable = [project for project in projects if project.lock is not None]
    for project in projects:
        if project.lock is None:
            log.warning(f"{project.lock_path} nicht lesbar: {project.problem}", project.lock_path)
    if projects:
        log.info(f"npm-Projekte: {joined({project.label for project in projects})}.")
    else:
        log.info("Keine package-lock.json im Repo, keine npm-Pakete zu prüfen.")

    check_npm_prerelease(readable, config, project_yaml, log)
    audited = audit_npm(readable, npm, config, project_yaml, runner, log)

    complete = len(readable) == len(projects)
    usage = {"prerelease": Usage(complete, used_keys(config.prerelease)),
             "advisories": Usage(complete and audited, used_keys(config.advisories))}
    scope = "npm"
    if state_file:
        nuget = load_state(state_file, "nuget")
        if nuget is None:
            log.info(f"Kein Ergebnis von „NuGet-Pakete prüfen“ in {state_file}.")
            nuget = {name: Usage(False) for name in LISTS}
        usage = {name: Usage(item.ran and nuget[name].ran, item.used | nuget[name].used) for name, item in usage.items()}
        scope = "NuGet und npm"
    warn_unused(config, project_yaml, usage, scope, log)


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
    state_help = ("Datei, über die packages und npm unbenutzte Ausnahmen gemeinsam beurteilen; packages schreibt "
                  "sie, npm liest sie")
    packages = commands.add_parser("packages", help="Vorabversionen, Lücken und veraltete Pakete")
    packages.add_argument("--solution", required=True)
    packages.add_argument("--project-yaml", default="project.yaml")
    packages.add_argument("--dotnet", default="dotnet")
    packages.add_argument("--state-file", help=state_help)
    npm = commands.add_parser("npm", help="Lücken und Vorabversionen der npm-Projekte im Repo")
    npm.add_argument("--project-yaml", default="project.yaml")
    npm.add_argument("--root", default=".")
    npm.add_argument("--npm", default="npm")
    npm.add_argument("--state-file", help=state_help)
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
        check_packages(args.dotnet, args.solution, args.project_yaml, runner, log, args.state_file)
    elif args.command == "npm":
        # Unter Windows (lokal) findet erst which npm.cmd.
        check_npm(Path(args.root), args.project_yaml, shutil.which(args.npm), runner, log, args.state_file)
    elif args.command == "suppressions":
        check_suppressions(Path(args.root).resolve(), log)
    else:
        check_format(args.dotnet, args.solution, runner, log, Path.cwd())
    if log.errors or log.warnings:
        log.info(f"{log.errors} Fehler, {log.warnings} {'Warnung' if log.warnings == 1 else 'Warnungen'}.")
    return 1 if log.errors else 0


if __name__ == "__main__":
    sys.exit(main())
