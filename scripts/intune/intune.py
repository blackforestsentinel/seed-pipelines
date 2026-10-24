#!/usr/bin/env python3
"""Sentinel Seed: die eine Intune-Richtlinie für die Edge-Erweiterungen aller Seed-Projekte.

  check  prüft die Registerdatei: Format und je Erweiterung, dass updates.xml erreichbar ist und
         die ID nennt (veröffentlicht?). Braucht keine Anmeldung, läuft auch im PR.
  plan   zeigt, was sich an der Richtlinie ändern würde; in Azure Pipelines setzt es die
         Ausgabevariablen changes und planId.
  apply  gleicht die Richtlinie an, liest sie danach erneut und prüft, dass nichts mehr fehlt.
         Mit --expect-plan nur, wenn der Plan noch derselbe ist wie bei der Freigabe.

Die Richtlinie (Einstellungskatalog, Windows, MDM) setzt nur die Edge-Richtlinie „Control which
extensions are installed silently“ (ExtensionInstallForcelist) auf „<id>;<updateUrl>“ je Eintrag
der Registerdatei, sortiert, und gilt für alle Benutzer. Intune erlaubt die Einstellung nur in einer
Richtlinie: Setzt eine zweite sie ebenfalls, meldet Intune einen Konflikt, und keine wirkt. Die
Pipeline besitzt deshalb die ganze Richtlinie; Einträge, die nicht in der Registerdatei stehen,
entfernt sie.

Übernahme: Fehlt die Richtlinie --policy-name, gibt es aber genau eine aus --adopt (etwa die alte
„Kundenportal: Edge-Erweiterung“), wird diese umbenannt und angeglichen, nie gelöscht und neu
angelegt: Edge würde die Erweiterungen dazwischen deinstallieren.

Braucht für plan und apply az, angemeldet als Identität mit der Graph-Berechtigung
DeviceManagementConfiguration.ReadWrite.All (Anwendung, onboarding/Initialize-SeedIntune.ps1).
Nur Standardbibliothek, Python ab 3.10.

  python3 scripts/intune/intune.py check --registry extensions.yaml
  python3 scripts/intune/intune.py plan --registry extensions.yaml --adopt "Kundenportal: Edge-Erweiterung"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

GRAPH = "https://graph.microsoft.com/beta"
POLICY_NAME = "Sentinel Seed: Edge-Erweiterungen"
MANAGED_BY = "der Seed-Pipeline aus templates/intune-extensions.yml (seed-pipelines)"
# Edge-Richtlinien im Einstellungskatalog; die ID hängt an der ADMX-Version, in der die
# Richtlinie dazukam. Die abhängige Liste liest das Skript aus der Definition selbst.
SETTING_CANDIDATES = (
    "device_vendor_msft_policy_config_microsoft_edgev77.3~policy~microsoft_edge~extensions_extensioninstallforcelist",
    "device_vendor_msft_policy_config_microsoft_edge~policy~microsoft_edge~extensions_extensioninstallforcelist",
)
FORCELIST_SUFFIX = "extensions_extensioninstallforcelist"
# Nur Edge zählt als Konflikt; Chrome (…~policy~googlechrome~…) hat eine eigene Forcelist.
EDGE_POLICY_MARKER = "~policy~microsoft_edge~"
ALL_USERS = "#microsoft.graph.allLicensedUsersAssignmentTarget"
EXCLUSION = "#microsoft.graph.exclusionGroupAssignmentTarget"

# Registerdatei
KEYS = ("name", "id", "updateUrl", "owner", "comment")
REQUIRED = ("name", "id", "updateUrl")
NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,38}[a-z0-9]")
ID_PATTERN = re.compile(r"[a-p]{32}")
UPDATES_PATH = "/api/extension/updates.xml"
GUPDATE = "{http://www.google.com/update2/response}"
MAX_MANIFEST_BYTES = 1_000_000

EMPTY_REGISTRY = (
    "Die Registerdatei enthält keine Erweiterung. Die Richtlinie bleibt unverändert: Eine leere Liste "
    "würde alle Erweiterungen auf allen verwalteten Geräten deinstallieren. Ist genau das gewollt, die "
    "Pipeline von Hand mit „Alle Erweiterungen entfernen“ (allowEmpty, --allow-empty) starten; dann "
    "löscht sie die Richtlinie.")


class IntuneError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def clean(message: str) -> str:
    """Ohne Steuerzeichen und ##vso[: Meldungen enthalten Antworten fremder APIs (updates.xml der
    Projekte), und der Agent führt Logging-Befehle in jeder Zeile aus, auch mitten im Text."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(message)).replace("##vso[", "##vso_[")


def warn(message: str) -> None:
    prefix = "##vso[task.logissue type=warning]" if os.environ.get("TF_BUILD") else "Warnung: "
    print(prefix + clean(message), flush=True)


def fail(*messages: str) -> None:
    """Meldet jeden Fehler einzeln (in Azure Pipelines als Annotation) und bricht ab."""
    messages = tuple(clean(message) for message in messages)
    if os.environ.get("TF_BUILD"):
        for message in messages:
            print(f"##vso[task.logissue type=error]{message}", flush=True)
    raise IntuneError("\n".join(messages))


def extensions_count(n: int) -> str:
    return f"{n} Erweiterung" if n == 1 else f"{n} Erweiterungen"


def set_output(name: str, value: str) -> None:
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.setvariable variable={name};isOutput=true]{value}", flush=True)


# --- Registerdatei ---------------------------------------------------------------------------
# Ein bewusst kleiner Ausschnitt aus YAML, den das Skript ohne PyYAML selbst liest:
#
#   extensions:
#     - name: kundenportal                      # Projekt
#       id: ponmlkjihgfedcbaponmlkjihgfedcba
#       updateUrl: https://func-….azurewebsites.net/api/extension/updates.xml
#       owner: team@example.com                 # optional
#       comment: Startseite und Seitenleiste    # optional
#
# Erlaubt sind Kommentare, Leerzeilen, „extensions: []“ und Werte ohne, in '…' oder in "…" (ohne
# Backslash). Alles andere (Tabulatoren, mehrzeilige Werte, {…}, Anker, unbekannte Schlüssel, Werte,
# die YAML nicht als Text läse, etwa yes oder 2026-01-01) ist ein Fehler mit Zeilennummer. Jede
# gültige Datei liest ein YAML-Parser genauso; die Tests prüfen das gegen PyYAML, wo es installiert ist.


@dataclass(frozen=True)
class Extension:
    name: str
    id: str
    update_url: str
    owner: str = ""
    comment: str = ""

    @property
    def entry(self) -> str:
        """Eintrag in ExtensionInstallForcelist."""
        return f"{self.id};{self.update_url}"


class RegistryError(IntuneError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


_ROOT_LINE = re.compile(r"extensions:(?P<rest>.*)")
_ITEM_LINE = re.compile(r"(?P<indent> *)-(?P<space> *)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)")
_FIELD_LINE = re.compile(r"(?P<indent> *)(?P<key>[A-Za-z_][A-Za-z0-9_]*):(?P<rest>.*)")
# Was YAML (1.1 wie PyYAML, dazu y/n) ohne Anführungszeichen nicht als Text liest.
_NOT_TEXT = re.compile(r"""
    ~ | null | Null | NULL
  | y | Y | yes | Yes | YES | n | N | no | No | NO
  | true | True | TRUE | false | False | FALSE | on | On | ON | off | Off | OFF
  | [-+]?[0-9][0-9_]*(\.[0-9_]*)?([eE][-+]?[0-9]+)?
  | [-+]?\.[0-9][0-9_]*([eE][-+]?[0-9]+)?
  | [-+]?\.(inf|Inf|INF) | \.(nan|NaN|NAN)
  | [-+]?0b[01_]+ | [-+]?0x[0-9a-fA-F_]+
  | [-+]?[0-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?
  | [0-9]{4}-[0-9]{1,2}-[0-9]{1,2}([Tt\ ].*)?
  | = | <<
""", re.VERBOSE)


def _scalar(rest: str) -> str:
    """Wert hinter „schlüssel:“: leer, ohne, in '…' oder in "…" Anführungszeichen, optional „ # …“."""
    if rest and not rest.startswith(" "):
        raise ValueError("nach dem Doppelpunkt fehlt ein Leerzeichen")
    text = rest.strip()
    if not text or text.startswith("#"):
        return ""
    if text[0] in "'\"":
        quote, start, parts = text[0], 1, []
        while True:
            end = text.find(quote, start)
            if end < 0:
                raise ValueError("Anführungszeichen nicht geschlossen (mehrzeilige Werte gibt es hier nicht)")
            parts.append(text[start:end])
            if quote == "'" and text[end + 1:end + 2] == "'":
                parts.append("'")
                start = end + 2
                continue
            break
        value, tail = "".join(parts), text[end + 1:].strip()
        if tail and not tail.startswith("#"):
            raise ValueError(f"nach dem schließenden Anführungszeichen steht noch „{tail}“")
        if quote == '"' and "\\" in value:
            raise ValueError("Backslash in \"…\" wird nicht unterstützt; einfache Anführungszeichen nehmen")
        return value
    cut = text.find(" #")
    value = (text if cut < 0 else text[:cut]).rstrip()
    if value[0] in "[]{}&*!|>%@`#,?:-" and not (value[0] in "-?:" and len(value) > 1 and value[1] != " "):
        raise ValueError(f"„{value}“ in Anführungszeichen setzen (beginnt mit einem YAML-Sonderzeichen)")
    if ": " in value or value.endswith(":"):
        raise ValueError(f"„{value}“ in Anführungszeichen setzen (enthält „: “)")
    if _NOT_TEXT.fullmatch(value):
        raise ValueError(f"„{value}“ in Anführungszeichen setzen; YAML läse den Wert sonst nicht als Text")
    return value


def url_problem(url: str) -> str | None:
    """Was an updateUrl nicht stimmt, oder None."""
    if any(c.isspace() for c in url) or ";" in url:
        return "darf weder Leerzeichen noch „;“ enthalten"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return "muss mit https:// und einem Host beginnen"
    if parts.username or parts.password:
        return "darf keine Anmeldedaten enthalten"
    if parts.query or parts.fragment:
        return "darf weder ?… noch #… enthalten"
    if not parts.path.endswith(UPDATES_PATH):
        return f"muss auf {UPDATES_PATH} enden (Update-Manifest der Seed-Erweiterung)"
    return None


def parse_registry(text: str, source: str = "Registerdatei") -> list[Extension]:
    problems: list[str] = []
    items: list[tuple[int, dict[str, str]]] = []
    state, item_indent = "root", -1

    def add(fields: dict[str, str], key: str, rest: str) -> None:
        if key not in KEYS:
            raise ValueError(f"unbekannter Schlüssel „{key}“; erlaubt sind {', '.join(KEYS)}")
        if key in fields:
            raise ValueError(f"„{key}“ steht in diesem Eintrag doppelt")
        fields[key] = _scalar(rest)

    for number, raw in enumerate(text.lstrip("﻿").splitlines(), start=1):
        if "\t" in raw:
            problems.append(f"{source}, Zeile {number}: Tabulator; nur mit Leerzeichen einrücken")
            continue
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        try:
            if state == "root":
                match = _ROOT_LINE.fullmatch(line)
                if not match:
                    raise ValueError("erwartet „extensions:“ am Zeilenanfang, den einzigen Schlüssel der Datei")
                rest = match["rest"]
                if re.fullmatch(r" +\[ *\]( +#.*)?", rest):
                    state = "empty"
                elif re.fullmatch(r"( +#.*)? *", rest):
                    state = "list"
                else:
                    raise ValueError("nach „extensions:“ folgen die Einträge in den nächsten Zeilen (oder „[]“ für keinen)")
                continue
            if state == "empty":
                raise ValueError("nach „extensions: []“ darf nichts mehr folgen")
            match = _ITEM_LINE.fullmatch(line)
            if match:
                if match["space"] != " ":
                    raise ValueError("nach „-“ genau ein Leerzeichen, dann der erste Schlüssel")
                indent = len(match["indent"])
                if item_indent < 0:
                    item_indent = indent
                elif indent != item_indent:
                    raise ValueError(f"Einträge („- “) einheitlich mit {item_indent} Leerzeichen einrücken")
                items.append((number, {}))
                add(items[-1][1], match["key"], match["rest"])
                continue
            match = _FIELD_LINE.fullmatch(line)
            if match and not match["indent"]:
                raise ValueError(f"Schlüssel „{match['key']}“ auf oberster Ebene; erlaubt ist nur „extensions“")
            if match and items:
                if len(match["indent"]) != item_indent + 2:
                    raise ValueError(f"Schlüssel eines Eintrags mit {item_indent + 2} Leerzeichen einrücken, "
                                     "bündig unter dem ersten Schlüssel nach „- “")
                add(items[-1][1], match["key"], match["rest"])
                continue
            raise ValueError("unerwartete Zeile; erlaubt sind Einträge „- name: …“ mit den Schlüsseln darunter")
        except ValueError as error:
            problems.append(f"{source}, Zeile {number}: {error}")
    if state == "root":
        problems.append(f"{source}: „extensions:“ fehlt")

    extensions: list[Extension] = []
    seen_names: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for number, fields in items:
        where = f"{source}, Eintrag ab Zeile {number}"
        missing = [key for key in REQUIRED if not fields.get(key)]
        if missing:
            problems.append(f"{where}: es fehlt {', '.join(missing)}")
            continue
        name, extension_id, url = fields["name"], fields["id"], fields["updateUrl"]
        if not NAME_PATTERN.fullmatch(name):
            problems.append(f"{where}: name „{name}“: 3 bis 40 Zeichen, Kleinbuchstaben, Ziffern und "
                            "Bindestriche, wie der Projektname in project.yaml")
        if not ID_PATTERN.fullmatch(extension_id):
            problems.append(f"{where}: id „{extension_id}“: 32 Kleinbuchstaben von a bis p (ID der "
                            "Erweiterung, folgt aus ihrem Signaturschlüssel)")
        problem = url_problem(url)
        if problem:
            problems.append(f"{where}: updateUrl „{url}“ {problem}")
        if name in seen_names:
            problems.append(f"{where}: name „{name}“ steht schon im Eintrag ab Zeile {seen_names[name]}")
        if extension_id in seen_ids:
            problems.append(f"{where}: id „{extension_id}“ steht schon im Eintrag ab Zeile {seen_ids[extension_id]}")
        seen_names.setdefault(name, number)
        seen_ids.setdefault(extension_id, number)
        extensions.append(Extension(name, extension_id, url, fields.get("owner", ""), fields.get("comment", "")))
    if problems:
        raise RegistryError(problems)
    return extensions


def read_registry(path: str) -> list[Extension]:
    file = Path(path)
    if not file.is_file():
        fail(f"Registerdatei {path} fehlt.")
    try:
        text = file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        fail(f"{path} ist nicht in UTF-8 gespeichert.")
    try:
        return parse_registry(text, file.name)
    except RegistryError as error:
        fail(*error.problems)


def desired_entries(extensions: list[Extension]) -> list[str]:
    """Soll-Liste: alle Einträge der Registerdatei, sortiert und eindeutig."""
    return sorted({extension.entry for extension in extensions})


# --- Veröffentlicht? ---------------------------------------------------------------------------


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sentinel-seed-intune", "Accept": "text/xml, application/xml"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError(f"leitet auf {response.geturl()} um, Edge braucht https")
        data = response.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("Antwort größer als 1 MB, das ist kein Update-Manifest")
    return data


def manifest_problem(xml: bytes, extension_id: str) -> str | None:
    """Was am Update-Manifest (gupdate, Protokoll 2.0) für diese ID nicht stimmt, oder None."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        return f"liefert kein gültiges XML ({error})"
    if root.tag != f"{GUPDATE}gupdate":
        return "liefert kein Update-Manifest (gupdate mit xmlns http://www.google.com/update2/response)"
    apps = root.findall(f"{GUPDATE}app")
    app = next((a for a in apps if a.get("appid") == extension_id), None)
    if app is None:
        found = ", ".join(a.get("appid") or "ohne appid" for a in apps) or "keine"
        return f"nennt nicht die ID {extension_id} (app/@appid: {found})"
    check = app.find(f"{GUPDATE}updatecheck")
    if check is None or not check.get("version") or not check.get("codebase"):
        return "nennt die ID, aber kein Paket (updatecheck mit version und codebase)"
    if not check.get("codebase", "").startswith("https://"):
        return f"nennt das Paket unter {check.get('codebase')}, Edge braucht https"
    return None


def check_published(extensions: list[Extension], get: Callable[[str], bytes] = fetch,
                    attempts: int = 3, delay: float = 5) -> list[str]:
    """Je Erweiterung: updates.xml erreichbar und app/@appid gleich id. Liefert die Probleme."""
    problems = []
    for extension in extensions:
        problem = None
        for attempt in range(1, attempts + 1):
            try:
                problem = manifest_problem(get(extension.update_url), extension.id)
                break
            except urllib.error.HTTPError as error:
                problem = f"antwortet mit {error.code}"
                if error.code < 500:
                    break
            except (OSError, ValueError) as error:
                problem = f"ist nicht erreichbar ({error})"
            if attempt < attempts:
                time.sleep(delay)
        if problem:
            problems.append(f"{extension.name} ({extension.id}): {extension.update_url} {problem}. "
                            "Erst veröffentlichen (Deploy des Projekts), dann eintragen.")
        else:
            log(f"  {extension.name}: {extension.id} wird unter {extension.update_url} ausgeliefert")
    return problems


# --- Graph ------------------------------------------------------------------------------------


class Graph:
    def __init__(self, token: str) -> None:
        self.token = token

    @classmethod
    def from_az(cls) -> "Graph":
        result = subprocess.run(
            ["az", "account", "get-access-token", "--resource-type", "ms-graph", "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True)
        if result.returncode != 0:
            fail(f"Kein Token für Microsoft Graph: {result.stderr.strip()}")
        return cls(result.stdout.strip())

    def request(self, method: str, path: str, body: object | None = None, missing_ok: bool = False):
        url = path if path.startswith("https://") else GRAPH + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        for attempt in range(1, 6):
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                if missing_ok and error.code in (400, 404):
                    return None
                if error.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(int(error.headers.get("Retry-After") or 5 * attempt))
                    continue
                detail = error.read().decode("utf-8", errors="replace")[:600]
                if error.code == 403:
                    detail += (" (fehlt DeviceManagementConfiguration.ReadWrite.All mit Administratorzustimmung? "
                               "onboarding/Initialize-SeedIntune.ps1)")
                fail(f"Graph {method} {path.split('?')[0]}: {error.code} {detail}")
            except urllib.error.URLError as error:
                if attempt < 5:
                    time.sleep(5 * attempt)
                    continue
                fail(f"Graph {method} {path.split('?')[0]}: nicht erreichbar ({error.reason})")

    def pages(self, path: str) -> list[dict]:
        items, url = [], path
        while url:
            page = self.request("GET", url)
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink")
        return items


def policy_path(policy: dict) -> str:
    return f"/deviceManagement/configurationPolicies('{policy['id']}')"


# --- Einstellung und Richtlinie ---------------------------------------------------------------


def find_definition(graph: Graph) -> dict:
    for candidate in SETTING_CANDIDATES:
        definition = graph.request("GET", f"/deviceManagement/configurationSettings('{urllib.parse.quote(candidate, safe='')}')", missing_ok=True)
        if definition:
            return definition
    fail("Die Einstellung ExtensionInstallForcelist (Microsoft Edge > Extensions > Control which extensions are "
         "installed silently) fehlt im Einstellungskatalog unter den bekannten IDs. Die ID in Intune nachsehen "
         f"und in SETTING_CANDIDATES ({Path(__file__).name}) eintragen.")


def setting_ids(definition: dict) -> tuple[str, str, str]:
    """Einstellung, ihr Wert „Aktiviert“ und die abhängige Liste der Erweiterungen."""
    enabled = next((o for o in definition.get("options", []) if str(o.get("itemId", "")).endswith("_1")), None)
    children = [d.get("dependedOnBy") for d in (enabled or {}).get("dependedOnBy", []) if d.get("dependedOnBy")]
    if not enabled or not children:
        fail(f"Unerwarteter Aufbau der Einstellung {definition.get('id')}: kein Wert „Aktiviert“ mit Liste.")
    child = next((c for c in children if c.endswith("desc")), children[0])
    return definition["id"], enabled["itemId"], child


def description(managed_by: str) -> str:
    return (f"Installiert die Edge-Erweiterungen der Seed-Projekte. Gepflegt von {managed_by}; "
            "Änderungen hier überschreibt der nächste Lauf.")


def desired_policy(ids: tuple[str, str, str], entries: list[str], name: str = POLICY_NAME,
                   managed_by: str = MANAGED_BY) -> dict:
    setting_id, enabled_id, list_id = ids
    return {
        "name": name,
        "description": description(managed_by),
        "platforms": "windows10",
        "technologies": "mdm",
        "roleScopeTagIds": ["0"],
        "settings": [{
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationSetting",
            "settingInstance": {
                "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance",
                "settingDefinitionId": setting_id,
                "choiceSettingValue": {
                    "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingValue",
                    "value": enabled_id,
                    "children": [{
                        "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingCollectionInstance",
                        "settingDefinitionId": list_id,
                        "simpleSettingCollectionValue": [
                            {"@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValue", "value": entry}
                            for entry in entries
                        ],
                    }],
                },
            },
        }],
    }


def entries_in(settings: list[dict], ids: tuple[str, str, str]) -> tuple[list[str] | None, int]:
    """Liste der Erweiterungen, wenn die Einstellung aktiviert ist, und die Zahl anderer Einstellungen."""
    setting_id, enabled_id, list_id = ids
    entries, others = None, 0
    for setting in settings:
        instance = setting.get("settingInstance", {})
        if instance.get("settingDefinitionId") != setting_id:
            others += 1
            continue
        choice = instance.get("choiceSettingValue", {})
        if choice.get("value") != enabled_id:
            continue
        entries = [value.get("value") for child in choice.get("children", []) if child.get("settingDefinitionId") == list_id
                   for value in child.get("simpleSettingCollectionValue", [])]
    return entries, others


def sets_forcelist(settings: list[dict], setting_id: str) -> bool:
    ids = {s.get("settingInstance", {}).get("settingDefinitionId", "") for s in settings}
    return setting_id in ids or any(i.endswith(FORCELIST_SUFFIX) and EDGE_POLICY_MARKER in i for i in ids)


def active_targets(assignments: list[dict]) -> list[dict]:
    """Zuweisungen, die Geräte oder Benutzer einschließen (Ausschlüsse zählen nicht)."""
    return [a for a in assignments if a.get("target", {}).get("@odata.type") != EXCLUSION]


@dataclass
class State:
    """Die Richtlinie in Intune, die die Pipeline besitzt (oder übernimmt), und alle anderen."""
    policy: dict | None = None
    adopted_from: str | None = None
    settings: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)
    others: list[dict] = field(default_factory=list)


def read_state(graph: Graph, name: str, adopt: list[str]) -> State:
    policies = graph.pages("/deviceManagement/configurationPolicies?$select=id,name,platforms,technologies&$top=100")
    ours = [p for p in policies if p.get("name") == name]
    if len(ours) > 1:
        fail(f"Es gibt {len(ours)} Richtlinien namens „{name}“ ({', '.join(p['id'] for p in ours)}); "
             "bitte in Intune auf eine reduzieren.")
    policy, adopted_from = (ours[0], None) if ours else (None, None)
    if policy is None and adopt:
        candidates = [p for p in policies if p.get("name") in adopt]
        if len(candidates) > 1:
            listed = ", ".join(f"„{p.get('name')}“ ({p['id']})" for p in candidates)
            fail(f"Die Richtlinie „{name}“ gibt es noch nicht, zum Übernehmen kommen aber mehrere in Frage: {listed}. "
                 "Übernommen wird nur genau eine, sonst ginge eine Liste verloren. Ihre Einträge in die "
                 "Registerdatei übernehmen, die übrigen Richtlinien in Intune löschen (oder adoptPolicies "
                 "einschränken) und neu starten.")
        if candidates:
            policy, adopted_from = candidates[0], candidates[0].get("name")
    if policy is not None:
        technologies = str(policy.get("technologies", "")).split(",")
        if policy.get("platforms") != "windows10" or "mdm" not in technologies:
            fail(f"Die Richtlinie „{policy.get('name')}“ ({policy['id']}) gilt für {policy.get('platforms')}/"
                 f"{policy.get('technologies')}, erwartet ist windows10/mdm. Das lässt sich nicht ändern: "
                 "Richtlinie in Intune umbenennen oder löschen.")
        settings = graph.pages(f"{policy_path(policy)}/settings")
        assignments = graph.pages(f"{policy_path(policy)}/assignments")
    else:
        settings, assignments = [], []
    others = [p for p in policies if policy is None or p.get("id") != policy["id"]]
    return State(policy, adopted_from, settings, assignments, others)


@dataclass
class Plan:
    steps: list[str] = field(default_factory=list)
    create: bool = False
    rename: bool = False
    replace_settings: bool = False
    assign: bool = False
    delete: bool = False
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Kennung des Plans; apply prüft damit, dass noch derselbe Plan gilt wie bei der Freigabe."""
        return hashlib.sha256("\n".join(self.steps).encode("utf-8")).hexdigest()[:16]


def make_plan(state: State, ids: tuple[str, str, str], entries: list[str], name: str = POLICY_NAME,
              allow_empty: bool = False) -> Plan:
    """Was zu tun ist, als lesbare Schritte; ohne Schritte stimmt alles."""
    plan = Plan()
    current, other_settings = entries_in(state.settings, ids)
    if not entries:
        if not allow_empty:
            fail(EMPTY_REGISTRY)
        if state.policy is not None and state.adopted_from is None:
            plan.delete = True
            plan.removed = list(current or [])
            plan.steps.append(f"Richtlinie „{name}“ löschen: Edge deinstalliert "
                              f"{extensions_count(len(plan.removed))} daraus")
        return plan
    if state.policy is None:
        plan.create = plan.assign = True
        plan.added = list(entries)
        plan.steps.append("\n".join([f"Richtlinie „{name}“ anlegen mit {extensions_count(len(entries))}"] +
                                    [f"+ {entry}" for entry in entries]))
        plan.steps.append("Zuweisen an alle Benutzer")
        return plan
    if state.adopted_from:
        plan.rename = True
        plan.steps.append(f"Richtlinie „{state.adopted_from}“ übernehmen und in „{name}“ umbenennen")
    if current != entries or other_settings:
        plan.replace_settings = True
        plan.added = [entry for entry in entries if entry not in (current or [])]
        plan.removed = [entry for entry in (current or []) if entry not in entries]
        lines = [f"Einstellung ersetzen, danach {extensions_count(len(entries))}"]
        if current is None:
            lines.append("bisher nicht aktiviert")
        lines += [f"+ {entry}" for entry in plan.added]
        lines += [f"- {entry} (Edge deinstalliert sie)" for entry in plan.removed]
        if current is not None and not plan.added and not plan.removed and current != entries:
            lines.append("Reihenfolge und doppelte Einträge angleichen")
        if other_settings:
            lines.append(f"{other_settings} weitere Einstellungen entfernen")
        plan.steps.append("\n".join(lines))
    targets = sorted(a.get("target", {}).get("@odata.type", "") for a in state.assignments)
    if targets != [ALL_USERS]:
        plan.assign = True
        plan.steps.append(f"Zuweisung ersetzen: {', '.join(t.split('.')[-1] for t in targets) or 'keine'} -> alle Benutzer")
    return plan


def find_conflicts(graph: Graph, others: list[dict], setting_id: str, adopt: list[str]) -> tuple[list[str], list[str]]:
    """Andere Richtlinien mit ExtensionInstallForcelist: zugewiesene als Fehler, übrige als Warnung.

    Zugewiesen heißt: Intune meldet auf den Geräten beider Richtlinien einen Konflikt, und keine
    wirkt; Anwenden hilft dann nicht, die Pipeline hält deshalb an. Ob sich die Zuweisungen
    tatsächlich überschneiden, prüft sie nicht (Gruppen); im Zweifel lieber anhalten. Eine Richtlinie
    ohne Zuweisung wirkt nirgends und ist nur ein Hinweis.
    """
    errors, warnings = [], []
    for policy in others:
        if "windows" not in str(policy.get("platforms", "")).lower():
            continue
        if not sets_forcelist(graph.pages(f"{policy_path(policy)}/settings"), setting_id):
            continue
        label = f"„{policy.get('name')}“ ({policy['id']})"
        hint = (" Sie steht in adoptPolicies, übernommen wird aber nur, solange es die Richtlinie der Pipeline "
                "noch nicht gibt." if policy.get("name") in adopt else "")
        if active_targets(graph.pages(f"{policy_path(policy)}/assignments")):
            errors.append(f"Die Intune-Richtlinie {label} setzt ebenfalls ExtensionInstallForcelist und ist zugewiesen. "
                          "Intune meldet dann einen Konflikt, und auf den betroffenen Geräten wirkt keine der beiden. "
                          "Ihre Einträge in die Registerdatei übernehmen und die Richtlinie in Intune löschen." + hint)
        else:
            warnings.append(f"Die Intune-Richtlinie {label} setzt ebenfalls ExtensionInstallForcelist, ist aber "
                            "nicht zugewiesen und wirkt deshalb nicht. Am besten in Intune löschen." + hint)
    return errors, warnings


# --- Befehle -----------------------------------------------------------------------------------


@dataclass
class Options:
    registry: str
    name: str = POLICY_NAME
    adopt: list[str] = field(default_factory=list)
    allow_empty: bool = False
    managed_by: str = MANAGED_BY
    expect_plan: str = ""


def run_check(options: Options, get: Callable[[str], bytes] = fetch, delay: float = 5) -> int:
    extensions = read_registry(options.registry)
    log(f"Registerdatei {options.registry}: {extensions_count(len(extensions))}, Format in Ordnung")
    if not extensions:
        warn(EMPTY_REGISTRY)
        return 0
    problems = check_published(extensions, get, delay=delay)
    if problems:
        fail(*problems)
    log("Alle Erweiterungen sind veröffentlicht.")
    return 0


def print_plan(plan: Plan, extensions: list[Extension], name: str) -> None:
    log(f"Richtlinie „{name}“, Soll: {extensions_count(len(extensions))}")
    for extension in sorted(extensions, key=lambda e: e.entry):
        log(f"  {extension.name}: {extension.entry}{f' ({extension.owner})' if extension.owner else ''}")
    if not plan.steps:
        log("Keine Änderungen.")
        return
    log("Änderungen:")
    for step in plan.steps:
        first, *rest = step.split("\n")
        log(f"  - {first}")
        for line in rest:
            log(f"      {line}")
    for entry in plan.removed:
        warn(f"Der Plan entfernt {entry}: Edge deinstalliert diese Erweiterung auf allen Geräten.")


def run_plan(options: Options, graph: Graph) -> int:
    extensions = read_registry(options.registry)
    entries = desired_entries(extensions)
    ids = setting_ids(find_definition(graph))
    # Ohne Einträge gibt es nichts zu übernehmen: höchstens die eigene Richtlinie wird gelöscht.
    state = read_state(graph, options.name, options.adopt if entries else [])
    plan = make_plan(state, ids, entries, options.name, options.allow_empty)
    print_plan(plan, extensions, options.name)
    errors, warnings = find_conflicts(graph, state.others, ids[0], options.adopt) if entries else ([], [])
    for message in warnings:
        warn(message)
    if errors:
        fail(*errors)
    set_output("changes", str(bool(plan.steps)).lower())
    set_output("planId", plan.id)
    log(f"Plan {plan.id}")
    return 0


def verify(graph: Graph, options: Options, ids: tuple[str, str, str], entries: list[str], attempts: int = 3,
           delay: float = 10) -> None:
    """Liest die Richtlinie erneut und prüft, dass nichts mehr zu tun ist."""
    for attempt in range(1, attempts + 1):
        state = read_state(graph, options.name, [])
        if not entries:
            remaining = [f"Richtlinie „{options.name}“ gibt es noch"] if state.policy else []
        else:
            remaining = make_plan(state, ids, entries, options.name).steps
        if not remaining:
            log("Richtlinie entspricht dem Soll.")
            return
        if attempt < attempts:
            time.sleep(delay)
    fail("Nach dem Anwenden fehlt noch: " + "; ".join(step.replace("\n", ", ") for step in remaining))


def run_apply(options: Options, graph: Graph, delay: float = 10) -> int:
    extensions = read_registry(options.registry)
    entries = desired_entries(extensions)
    ids = setting_ids(find_definition(graph))
    state = read_state(graph, options.name, options.adopt if entries else [])
    plan = make_plan(state, ids, entries, options.name, options.allow_empty)
    if not plan.steps:
        log("Keine Änderungen.")
        return 0
    if options.expect_plan and plan.id != options.expect_plan:
        print_plan(plan, extensions, options.name)
        fail(f"Der Plan hat sich seit der Freigabe geändert (freigegeben {options.expect_plan}, jetzt {plan.id}), "
             "etwa weil jemand die Richtlinie in Intune geändert hat. Nichts angewendet; bitte einen neuen Lauf "
             "starten und dessen Plan freigeben.")
    print_plan(plan, extensions, options.name)
    if plan.delete:
        graph.request("DELETE", policy_path(state.policy))
        log(f"Gelöscht: {options.name}")
        verify(graph, options, ids, entries, delay=delay)
        return 0
    body = desired_policy(ids, entries, options.name, options.managed_by)
    policy = state.policy
    if plan.create:
        policy = graph.request("POST", "/deviceManagement/configurationPolicies", body)
        log(f"Angelegt: {options.name} ({policy['id']})")
    elif plan.rename or plan.replace_settings:
        graph.request("PUT", policy_path(policy), body)
        log(f"Übernommen und umbenannt: {state.adopted_from} -> {options.name}" if plan.rename else "Einstellung ersetzt.")
    if plan.assign:
        graph.request("POST", f"{policy_path(policy)}/assign", {"assignments": [{"target": {"@odata.type": ALL_USERS}}]})
        log("Zugewiesen an alle Benutzer.")
    verify(graph, options, ids, entries, delay=delay)
    return 0


def parse_args(argv: list[str]) -> tuple[str, Options]:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "plan", "apply"])
    parser.add_argument("--registry", required=True, help="Registerdatei, z. B. extensions.yaml")
    parser.add_argument("--policy-name", default=POLICY_NAME, help=f"Name der Richtlinie, Standard „{POLICY_NAME}“")
    parser.add_argument("--adopt", action="append", default=[], metavar="NAME",
                        help="Alte Richtlinie, die übernommen wird, wenn es --policy-name noch nicht gibt (mehrfach)")
    parser.add_argument("--adopt-json", default="", metavar="JSON", help="Dasselbe als JSON-Liste, etwa aus der Pipeline")
    parser.add_argument("--allow-empty", action="store_true",
                        help="Leere Registerdatei anwenden: löscht die Richtlinie, Edge deinstalliert alle Erweiterungen")
    parser.add_argument("--managed-by", default=MANAGED_BY, help="Wer die Richtlinie pflegt, für die Beschreibung")
    parser.add_argument("--expect-plan", default="", metavar="ID", help="apply nur, wenn der Plan noch diese Kennung hat")
    args = parser.parse_args(argv)
    adopt = list(args.adopt)
    if args.adopt_json.strip():
        try:
            extra = json.loads(args.adopt_json)
        except ValueError as error:
            fail(f"--adopt-json ist kein JSON: {error}")
        if not isinstance(extra, list) or not all(isinstance(n, str) for n in extra):
            fail("--adopt-json: erwartet eine Liste von Namen, z. B. [\"Kundenportal: Edge-Erweiterung\"].")
        adopt += extra
    adopt = [n.strip() for n in adopt if n.strip()]
    if not args.policy_name.strip():
        fail("--policy-name darf nicht leer sein.")
    if args.policy_name in adopt:
        fail(f"„{args.policy_name}“ ist der Name der Richtlinie selbst und gehört nicht in adoptPolicies.")
    return args.command, Options(args.registry, args.policy_name, adopt, args.allow_empty, args.managed_by,
                                 args.expect_plan.strip())


def main(argv: list[str], graph_factory: Callable[[], Graph] = Graph.from_az,
         get: Callable[[str], bytes] = fetch) -> int:
    command, options = parse_args(argv)
    if command == "check":
        return run_check(options, get)
    graph = graph_factory()
    return run_plan(options, graph) if command == "plan" else run_apply(options, graph)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except IntuneError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)
