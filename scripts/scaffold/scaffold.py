#!/usr/bin/env python3
"""Sentinel Seed: zentrales Scaffold, die Schritte gegen Azure DevOps und den Terraform-State.

  check    prüft vor jeder Änderung die Eingaben, das Zielprojekt (Muster; erreichbar heißt
           freigeschaltet), die Mitgliedschaft der startenden Person, das Repo je Modus und die
           Namen der Service Connections im Zielprojekt. Setzt in Azure Pipelines die Variablen
           seedProjectId, seedProjectName, seedRepositoryId und seedDefaultBranch.
  reserve  prüft, dass der Projektname frei ist, und trägt ihn in die Registry im Container des
           Terraform-States ein (scaffold/registry/<name>.json); ein erneuter Lauf für dasselbe
           Azure-DevOps-Projekt ist in Ordnung. Braucht az, angemeldet mit Storage Blob Data
           Contributor auf dem Container und Reader auf der Subscription.
  share    teilt die Service Connections des zentralen Projekts ins Zielprojekt, unter demselben
           Namen. Wer teilt, wird dort Administrator der Service Connection und darf die neue
           Pipeline berechtigen.
  run      startet den ersten Lauf einer Pipeline im Zielprojekt.

Anmeldung bei Azure DevOps mit dem Job-Token (SYSTEM_ACCESSTOKEN), Organisation aus
SYSTEM_COLLECTIONURI, zentrales Projekt aus SYSTEM_TEAMPROJECTID. Hinter dem Token steht die
Build-Service-Identität des zentralen Projekts; ein Projektadmin schaltet sie im Zielprojekt frei
(onboarding/Grant-SeedScaffoldAccess.ps1). Eingaben der startenden Person kommen als Argumente aus
Umgebungsvariablen, nie als Text im Skript. Nur Standardbibliothek, Python ab 3.10.

  python3 scripts/scaffold/scaffold.py check --name kundenportal --mode new --ado-project Projekt-0042 \\
      --repository kundenportal --connection sc-seed --connection github-example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

MODES = ("new", "migrate", "enableBranchPolicy")
NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,18}[a-z0-9]")
# Repo-Namen in Azure Repos sind freier; das Scaffold nimmt nur unverfängliche.
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Projektnamen in Azure DevOps: ohne \ / : * ? " < > ; # $ { } , + = [ ] | und Steuerzeichen
PROJECT_PATTERN = re.compile(r"[^\\/:*?\"<>;#${},+=\[\]|\x00-\x1f]{1,64}")
SCAFFOLD_MARKER = "seed-scaffold:"
REGISTRY_PREFIX = "scaffold/registry/"
API = "api-version=7.1"
API_PREVIEW = "api-version=7.1-preview.1"


class ScaffoldError(Exception):
    pass


def clean(value: object) -> str:
    """Für Meldungen: ohne Zeilenumbrüche, Steuerzeichen und ##vso[, damit keine Logging-Befehle
    entstehen. Der Agent erkennt sie in jeder Zeile, auch mitten im Text, etwa in "Fehler: …"."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value))[:200].replace("##vso[", "##vso_[")


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    prefix = "##vso[task.logissue type=warning]" if os.environ.get("TF_BUILD") else "Warnung: "
    print(prefix + message, flush=True)


def fail(*messages: str) -> None:
    """Meldet jeden Fehler einzeln (in Azure Pipelines als Annotation) und bricht ab."""
    if os.environ.get("TF_BUILD"):
        for message in messages:
            print(f"##vso[task.logissue type=error]{message}", flush=True)
    raise ScaffoldError("\n".join(messages))


def set_variable(name: str, value: str) -> None:
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.setvariable variable={name}]{value}", flush=True)


# --- Azure DevOps ------------------------------------------------------------------------------

class AzureDevOps:
    def __init__(self, organization_url: str, token: str) -> None:
        self.org = organization_url.rstrip("/")
        name = self.org.rsplit("/", 1)[-1]
        self.vssps = f"https://vssps.dev.azure.com/{name}"
        self.token = token

    @classmethod
    def from_environment(cls) -> "AzureDevOps":
        org = os.environ.get("SYSTEM_COLLECTIONURI", "")
        token = os.environ.get("SYSTEM_ACCESSTOKEN", "")
        if not org or not token or token.startswith("$("):
            fail("SYSTEM_COLLECTIONURI und SYSTEM_ACCESSTOKEN fehlen (env: SYSTEM_ACCESSTOKEN: $(System.AccessToken)).")
        return cls(org, token)

    def request(self, method: str, url: str, body: object | None = None, missing_ok: bool = False):
        data = None if body is None else json.dumps(body).encode("utf-8")
        for attempt in range(1, 6):
            request = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = response.read()
                    if response.status == 203:
                        fail(f"Azure DevOps {method} {url.split('?')[0]}: nicht angemeldet (203).")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as error:
                if missing_ok and error.code in (400, 401, 403, 404):
                    return None
                if error.code in (429, 500, 502, 503, 504) and attempt < 5:
                    time.sleep(int(error.headers.get("Retry-After") or 5 * attempt))
                    continue
                detail = error.read().decode("utf-8", errors="replace")[:400]
                fail(f"Azure DevOps {method} {url.split('?')[0]}: {error.code} {clean(detail)}")
            except urllib.error.URLError as error:
                if attempt < 5:
                    time.sleep(5 * attempt)
                    continue
                fail(f"Azure DevOps {method} {url.split('?')[0]}: nicht erreichbar ({error.reason})")


def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def descriptor(value: object) -> str:
    if isinstance(value, dict):
        return f"{value.get('identityType')};{value.get('identifier')}".lower()
    return str(value).lower()


# --- check -------------------------------------------------------------------------------------

@dataclass
class CheckOptions:
    name: str
    mode: str
    ado_project: str
    repository: str
    allowed_projects: str
    member_group: str
    requester_id: str
    connections: list[str]


def input_problems(options: CheckOptions) -> list[str]:
    problems = []
    if not NAME_PATTERN.fullmatch(options.name):
        problems.append("name: 3-20 Zeichen, Kleinbuchstaben, Ziffern und Bindestriche, beginnt mit einem Buchstaben.")
    if options.mode not in MODES:
        problems.append(f"mode: einer von {', '.join(MODES)}.")
    if not PROJECT_PATTERN.fullmatch(options.ado_project) or options.ado_project.startswith(("_", ".")):
        problems.append("adoProject: Name eines Azure-DevOps-Projekts.")
    if not REPOSITORY_PATTERN.fullmatch(options.repository):
        problems.append("repository: Name des Repos im Zielprojekt (Buchstaben, Ziffern, Punkt, Bindestrich, Unterstrich).")
    if options.allowed_projects:
        try:
            re.compile(options.allowed_projects)
        except re.error as error:
            problems.append(f"allowedProjects ist kein gültiger regulärer Ausdruck: {error}")
    return problems


def find_project(ado: AzureDevOps, name: str) -> dict:
    project = ado.request("GET", f"{ado.org}/_apis/projects/{q(name)}?{API}", missing_ok=True)
    if not project:
        fail(f"Azure-DevOps-Projekt {clean(name)} nicht gefunden, oder das zentrale Scaffold ist dort nicht "
             "freigeschaltet: Ein Projektadmin führt einmal onboarding/Grant-SeedScaffoldAccess.ps1 aus.")
    return project


def is_member(ado: AzureDevOps, requester_id: str, group_name: str) -> bool:
    people = ado.request("GET", f"{ado.vssps}/_apis/identities?identityIds={q(requester_id)}&queryMembership=ExpandedUp&{API}")
    person = (people or {}).get("value") or []
    if not person or person[0] is None:
        fail("Die Person, die den Lauf gestartet hat, ist in Azure DevOps nicht zu finden (Build.RequestedForId).")
    member_of = {descriptor(d) for d in person[0].get("memberOf") or []}
    groups = ado.request("GET", f"{ado.vssps}/_apis/identities?searchFilter=General&filterValue={q(group_name)}&queryMembership=None&{API}")
    matches = [g for g in (groups or {}).get("value") or []
               if g and g.get("isContainer") and str(g.get("providerDisplayName", "")).lower() == group_name.lower()]
    if len(matches) != 1:
        fail(f"Gruppe {clean(group_name)} nicht eindeutig gefunden ({len(matches)} Treffer).")
    return descriptor(matches[0]["descriptor"]) in member_of


def check_repository(ado: AzureDevOps, project: dict, repository: str, mode: str) -> dict:
    api = f"{ado.org}/{project['id']}/_apis/git/repositories"
    repo = ado.request("GET", f"{api}/{q(repository)}?{API}", missing_ok=True)
    if not repo:
        hint = ("Bei new zuerst das Repo im Zielprojekt anlegen (Repos, New repository, mit README); "
                "das Scaffold darf keine Repos anlegen.") if mode == "new" else ""
        fail(f"Repo {clean(repository)} gibt es im Projekt {clean(project['name'])} nicht. {hint}".strip())
    branch = repo.get("defaultBranch") or ""
    if not branch:
        fail(f"Repo {clean(repository)} ist leer. Es braucht mindestens einen Commit, etwa ein README.")
    if mode == "new":
        if branch != "refs/heads/main":
            fail(f"Repo {clean(repository)}: Default-Branch ist {clean(branch)}, erwartet ist main.")
        yaml = ado.request("GET", f"{api}/{repo['id']}/items?path=%2Fproject.yaml&versionDescriptor.version=main&{API}",
                           missing_ok=True)
        if yaml:
            commits = ado.request("GET", f"{api}/{repo['id']}/commits?searchCriteria.itemVersion.version=main"
                                         f"&searchCriteria.$top=100&{API}") or {}
            if not any(str(c.get("comment", "")).startswith(SCAFFOLD_MARKER) for c in commits.get("value") or []):
                fail(f"Repo {clean(repository)} enthält schon project.yaml, aber nicht von seed-scaffold. "
                     "Ein bestehendes Projekt mit mode migrate einrichten.")
            log(f"Repo {repository}: seed-template ist schon eingeführt (früherer Lauf).")
    elif branch != "refs/heads/main":
        warn(f"Repo {clean(repository)}: Default-Branch ist {clean(branch)}. Die Branch-Prüfung an der Service "
             "Connection erlaubt zentral meist nur main; der Lauf auf diesem Branch braucht dort einen Eintrag.")
    return repo


def endpoint_named(ado: AzureDevOps, project_id: str, name: str) -> dict | None:
    data = ado.request("GET", f"{ado.org}/{project_id}/_apis/serviceendpoint/endpoints?endpointNames={q(name)}&{API}")
    return next((e for e in (data or {}).get("value") or [] if e.get("name") == name), None)


def connection_problems(ado: AzureDevOps, central_id: str, target_id: str, names: list[str]) -> list[str]:
    problems = []
    for name in names:
        central = endpoint_named(ado, central_id, name)
        if not central:
            problems.append(f"Service Connection {name} fehlt im zentralen Projekt.")
            continue
        if target_id == central_id:
            continue
        target = endpoint_named(ado, target_id, name)
        if target and target.get("id") != central.get("id"):
            problems.append(f"Im Zielprojekt heißt schon eine andere Service Connection {name}; "
                            "sie umbenennen oder das Projekt ohne Selbstbedienung einrichten.")
    return problems


def run_check(ado: AzureDevOps, options: CheckOptions, central_id: str) -> int:
    problems = input_problems(options)
    if problems:
        fail(*problems)
    project = find_project(ado, options.ado_project)
    if options.allowed_projects and not re.fullmatch(options.allowed_projects, project["name"]):
        fail(f"Projekt {clean(project['name'])} ist für das zentrale Scaffold nicht vorgesehen (allowedProjects).")
    group = f"[{project['name']}]\\{options.member_group}"
    if not is_member(ado, options.requester_id, group):
        fail(f"Wer den Lauf startet, muss im Zielprojekt Mitglied von {clean(group)} sein.")
    log(f"Zielprojekt {project['name']}, Mitgliedschaft in {group}: ja")
    repo = check_repository(ado, project, options.repository, options.mode)
    problems = connection_problems(ado, central_id, project["id"], options.connections)
    if problems:
        fail(*problems)
    log(f"Repo {repo['name']} ({repo['defaultBranch']}), Service Connections: {', '.join(options.connections)}")
    set_variable("seedProjectId", project["id"])
    # Der Name aus der API darf in Skripte (Terraform-Werte): Azure DevOps lässt in Projektnamen
    # weder Anführungszeichen noch Steuerzeichen zu.
    set_variable("seedProjectName", project["name"])
    set_variable("seedRepositoryId", repo["id"])
    set_variable("seedDefaultBranch", repo["defaultBranch"].removeprefix("refs/heads/"))
    return 0


# --- share -------------------------------------------------------------------------------------

def run_share(ado: AzureDevOps, central_id: str, target_id: str, names: list[str]) -> int:
    if target_id == central_id:
        log("Zielprojekt ist das zentrale Projekt; nichts zu teilen.")
        return 0
    project = ado.request("GET", f"{ado.org}/_apis/projects/{q(target_id)}?{API}")
    for name in names:
        central = endpoint_named(ado, central_id, name)
        if not central:
            fail(f"Service Connection {name} fehlt im zentralen Projekt.")
        target = endpoint_named(ado, target_id, name)
        if target:
            if target.get("id") != central.get("id"):
                fail(f"Im Zielprojekt heißt schon eine andere Service Connection {name}.")
            log(f"{name}: schon geteilt")
            continue
        ado.request("PATCH", f"{ado.org}/_apis/serviceendpoint/endpoints/{central['id']}?{API}", [{
            "name": name,
            "description": "Sentinel Seed: geteilt vom zentralen Scaffold",
            "projectReference": {"id": target_id, "name": project["name"]},
        }])
        if not endpoint_named(ado, target_id, name):
            fail(f"{name} ist nach dem Teilen im Zielprojekt nicht zu sehen.")
        log(f"{name}: geteilt")
    return 0


# --- run ---------------------------------------------------------------------------------------

def run_first_run(ado: AzureDevOps, target_id: str, pipeline_id: str) -> int:
    if not pipeline_id.isdigit():
        fail(f"Pipeline-ID fehlt oder ist ungültig: {clean(pipeline_id)}")
    run = ado.request("POST", f"{ado.org}/{target_id}/_apis/pipelines/{pipeline_id}/runs?{API}", {})
    log(f"Erster Lauf: {run['_links']['web']['href']}")
    return 0


# --- reserve -----------------------------------------------------------------------------------

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def az_runner(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["az", *args], capture_output=True, text=True)


@dataclass
class ReserveOptions:
    name: str
    mode: str
    account: str
    container: str
    project_id: str
    project_name: str
    repository: str
    environments: list[str]
    requested_by: str
    build_id: str
    dry_run: bool


class Storage:
    def __init__(self, runner: Runner, account: str, container: str) -> None:
        self.runner, self.account, self.container = runner, account, container

    def _az(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return self.runner(["storage", "blob", *args, "--auth-mode", "login",
                            "--account-name", self.account, "--container-name", self.container])

    def exists(self, blob: str) -> bool:
        result = self._az("exists", "--name", blob, "--query", "exists", "-o", "tsv")
        if result.returncode:
            fail(f"State-Container nicht lesbar: {clean(result.stderr.strip())}")
        return result.stdout.strip().lower() == "true"

    def read(self, blob: str) -> str | None:
        if not self.exists(blob):
            return None
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "blob.json")
            result = self._az("download", "--name", blob, "--file", path, "--no-progress", "-o", "none")
            if result.returncode:
                fail(f"{blob} nicht lesbar: {clean(result.stderr.strip())}")
            with open(path, encoding="utf-8") as handle:
                return handle.read()

    def any_with_prefix(self, prefix: str) -> bool:
        result = self._az("list", "--prefix", prefix, "--num-results", "1", "--query", "[].name", "-o", "tsv")
        if result.returncode:
            fail(f"State-Container nicht lesbar: {clean(result.stderr.strip())}")
        return bool(result.stdout.strip())

    def create(self, blob: str, content: str) -> bool:
        """Legt den Blob nur an, wenn es ihn nicht gibt; False, wenn ihn jemand gleichzeitig angelegt hat."""
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "blob.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            result = self._az("upload", "--name", blob, "--file", path, "--if-none-match", "*",
                              "--content-type", "application/json", "--no-progress", "-o", "none")
        if result.returncode == 0:
            return True
        if re.search(r"BlobAlreadyExists|ConditionNotMet|already exists|412", result.stderr):
            return False
        fail(f"{blob} nicht schreibbar: {clean(result.stderr.strip())}")


def resource_groups(runner: Runner, name: str, environments: list[str]) -> list[str]:
    names = json.dumps([f"rg-{name}-{env}" for env in environments]).replace('"', "'")
    query = f"[?tags.\"seed:project\"=='{name}' || contains({names}, name)].name"
    result = runner(["group", "list", "--query", query, "-o", "tsv"])
    if result.returncode:
        fail(f"Resource Groups nicht lesbar (Reader auf der Subscription?): {clean(result.stderr.strip())}")
    return [line for line in result.stdout.split() if line]


def registry_owner(entry: str) -> dict:
    try:
        data = json.loads(entry)
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def run_reserve(options: ReserveOptions, runner: Runner = az_runner) -> int:
    if not NAME_PATTERN.fullmatch(options.name):
        fail("name: 3-20 Zeichen, Kleinbuchstaben, Ziffern und Bindestriche, beginnt mit einem Buchstaben.")
    storage = Storage(runner, options.account, options.container)
    blob = f"{REGISTRY_PREFIX}{options.name}.json"

    def accept_existing(entry: str) -> int:
        owner = registry_owner(entry)
        if owner.get("projectId") != options.project_id:
            fail(f"Der Name {options.name} gehört schon zum Azure-DevOps-Projekt {clean(owner.get('adoProject', '?'))}.")
        log(f"Name {options.name} ist schon für {options.project_name} eingetragen.")
        return 0

    existing = storage.read(blob)
    if existing is not None:
        return accept_existing(existing)
    if options.mode == "enableBranchPolicy":
        warn(f"Name {options.name} steht nicht in der Registry (Projekt ohne zentrales Scaffold eingerichtet?).")
        return 0
    taken = []
    if storage.any_with_prefix(f"{options.name}/"):
        taken.append(f"Terraform-State unter {options.name}/")
    if storage.exists(f"scaffold/{options.name}.tfstate"):
        taken.append(f"Scaffold-State scaffold/{options.name}.tfstate")
    groups = resource_groups(runner, options.name, options.environments)
    if groups:
        taken.append("Resource Groups " + ", ".join(groups))
    if taken:
        fail(f"Der Name {options.name} ist schon vergeben: " + "; ".join(taken) + ".")
    if options.dry_run:
        log(f"Name {options.name} ist frei (dryRun: nicht eingetragen).")
        return 0
    entry = json.dumps({
        "name": options.name,
        "adoProject": options.project_name,
        "projectId": options.project_id,
        "repository": options.repository,
        "mode": options.mode,
        "requestedBy": options.requested_by,
        "buildId": options.build_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, indent=2)
    if not storage.create(blob, entry):
        return accept_existing(storage.read(blob) or "")
    log(f"Name {options.name} für {options.project_name} eingetragen ({blob}).")
    return 0


# --- Aufruf ------------------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Eingaben, Zielprojekt, Mitgliedschaft, Repo, Service Connections")
    check.add_argument("--name", required=True)
    check.add_argument("--mode", required=True)
    check.add_argument("--ado-project", required=True)
    check.add_argument("--repository", required=True)
    check.add_argument("--allowed-projects", default="", help="Regulärer Ausdruck für erlaubte Zielprojekte")
    check.add_argument("--member-group", default="Contributors", help="Gruppe im Zielprojekt, Standard Contributors")
    check.add_argument("--requester-id", default=os.environ.get("BUILD_REQUESTEDFORID", ""))
    check.add_argument("--connection", action="append", default=[], help="Name einer Service Connection (mehrfach)")

    reserve = sub.add_parser("reserve", help="Projektname prüfen und in der Registry eintragen")
    reserve.add_argument("--name", required=True)
    reserve.add_argument("--mode", required=True, choices=MODES)
    reserve.add_argument("--storage-account", required=True)
    reserve.add_argument("--container", required=True)
    reserve.add_argument("--project-id", required=True)
    reserve.add_argument("--project-name", required=True)
    reserve.add_argument("--repository", default="")
    reserve.add_argument("--environments-json", default="[]")
    reserve.add_argument("--requested-by", default=os.environ.get("BUILD_REQUESTEDFOR", ""))
    reserve.add_argument("--build-id", default=os.environ.get("BUILD_BUILDID", ""))
    reserve.add_argument("--dry-run", action="store_true")

    share = sub.add_parser("share", help="Service Connections ins Zielprojekt teilen")
    share.add_argument("--project-id", required=True)
    share.add_argument("--connection", action="append", default=[])

    run = sub.add_parser("run", help="Ersten Lauf im Zielprojekt starten")
    run.add_argument("--project-id", required=True)
    run.add_argument("--pipeline-id", required=True)
    return parser.parse_args(argv)


def environments_from_json(text: str) -> list[str]:
    try:
        data = json.loads(text or "[]")
    except ValueError as error:
        fail(f"environments ist kein JSON: {error}")
    if not isinstance(data, list) or not all(isinstance(e, str) for e in data):
        fail("environments: erwartet eine Liste von Namen, z. B. [dev, prod].")
    return data


def main(argv: list[str], ado_factory: Callable[[], AzureDevOps] = AzureDevOps.from_environment,
         runner: Runner = az_runner) -> int:
    args = parse_args(argv)
    if args.command == "reserve":
        return run_reserve(ReserveOptions(
            args.name, args.mode, args.storage_account, args.container, args.project_id, args.project_name,
            args.repository, environments_from_json(args.environments_json), args.requested_by, args.build_id,
            args.dry_run), runner)
    ado = ado_factory()
    central_id = os.environ.get("SYSTEM_TEAMPROJECTID", "")
    if not central_id:
        fail("SYSTEM_TEAMPROJECTID fehlt.")
    if args.command == "check":
        return run_check(ado, CheckOptions(args.name, args.mode, args.ado_project, args.repository,
                                           args.allowed_projects, args.member_group, args.requester_id,
                                           args.connection), central_id)
    if args.command == "share":
        return run_share(ado, central_id, args.project_id, args.connection)
    return run_first_run(ado, args.project_id, args.pipeline_id)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ScaffoldError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)
