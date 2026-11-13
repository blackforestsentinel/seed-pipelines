#!/usr/bin/env python3
"""Sentinel Seed: zentrales Scaffold, die Schritte gegen Azure DevOps, Azure und den Terraform-State.

  check           prüft vor jeder Änderung die Eingaben, das Zielprojekt (Muster; erreichbar heißt
                  freigeschaltet), die Mitgliedschaft der startenden Person, das Repo je Modus und
                  die Namen der Service Connections im zentralen Projekt. Setzt in Azure Pipelines
                  die Variablen seedProjectId, seedProjectName, seedRepository, seedRepositoryId
                  und seedDefaultBranch.
  create-project  legt ein Azure-DevOps-Projekt an (Modus createProject) und nimmt die startende
                  Person als Contributor auf; gibt es das Projekt schon, prüft es nur.
  reserve         prüft, dass der Projektname frei ist, und trägt ihn in die Registry im Container
                  des Terraform-States ein (scaffold/registry/<name>.json).
  identity        legt in Azure die Identität des Projekts an: Managed Identity id-<name>, Resource
                  Groups rg-<name>-<env>, Rollen nur darauf, State nur unter <name>/, Graph-Rolle
                  nur mit sso. identity federate hängt danach das Federated Credential für die
                  Service Connection des Projekts an.
  fill            bringt seed-template an einem Tag als einen Commit nach main des Zielrepos.
  connection      legt im Modus migrate die Service Connection sc-<name> mit Prüfungen an (bei
                  new übernimmt das Terraform).
  verify          prüft vor dem Berechtigen, dass main nur den Scaffold-Commit und die Commits von
                  Terraform enthält.
  share           teilt die GitHub-Service-Connection des zentralen Projekts ins Zielprojekt.
  run             berechtigt die neue Pipeline für ihre Service Connections und startet den ersten
                  Lauf.

Anmeldung bei Azure DevOps mit einem Bearer-Token aus SEED_AZDO_TOKEN (sonst SYSTEM_ACCESSTOKEN):
dem Job-Token (azureDevOpsAuth jobToken) oder einem Entra-Token der Scaffold-Identität
(servicePrincipal). Organisation aus SYSTEM_COLLECTIONURI, zentrales Projekt aus
SYSTEM_TEAMPROJECTID. Azure über az, angemeldet mit der Scaffold-Identität. Code aus dem Zielrepo
oder aus seed-template führt das Skript nie aus. Eingaben der startenden Person kommen als
Argumente aus Umgebungsvariablen, nie als Text im Skript. Nur Standardbibliothek, Python ab 3.10.

  python3 scripts/scaffold/scaffold.py check --name kundenportal --mode new --ado-project Projekt-0042 \\
      --repository kundenportal --connection github-example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

MODES = ("createProject", "new", "migrate", "enableBranchPolicy")
AUTH_MODES = ("jobToken", "servicePrincipal")
NAME_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,18}[a-z0-9]")
# Präfixe im State-Container, die nicht einem Projekt gehören: Ein Projekt "plans" bekäme mit
# seinem Pfad <name>/ sonst die Pläne aller Projekte, eines namens "scaffold" die Registry.
RESERVED_NAMES = frozenset({"scaffold", "plans", "registry"})
ENVIRONMENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,19}")
# Repo-Namen in Azure Repos sind freier; das Scaffold nimmt nur unverfängliche.
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Projektnamen in Azure DevOps: ohne \ / : * ? " < > ; # $ { } , + = [ ] | und Steuerzeichen
PROJECT_PATTERN = re.compile(r"[^\\/:*?\"<>;#${},+=\[\]|\x00-\x1f]{1,64}")
TEMPLATE_REF_PATTERN = re.compile(r"v\d+\.\d+\.\d+")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
SCAFFOLD_MARKER = "seed-scaffold:"
TEMPLATE_MARKER = "seed-scaffold: seed-template"
# Was "Initialize" in Azure Repos anlegt (README, .gitignore) und eine Lizenz: Mehr darf main bei
# new nicht enthalten, sonst ist es ein bestehendes Projekt (migrate).
INITIAL_FILES = frozenset({"readme", "readme.md", "readme.txt", "license", "license.md", "license.txt", ".gitignore"})
# Dateien, die Terraform (ado-project) nach dem Scaffold-Commit auf main schreibt.
TERRAFORM_FILES = ("/project.yaml", "/azure-pipelines.yml")
REGISTRY_PREFIX = "scaffold/registry/"
API = "api-version=7.1"
API_PREVIEW = "api-version=7.1-preview.1"
SEED_PIPELINES_REPOSITORY = "blackforestsentinel/seed-pipelines"
WEB_APP_TEMPLATE = "templates/web-app.yml"
BRANCH_CONTROL_CHECK = "fe1de3ee-a436-41b4-bb20-f6eb4cb879a7"
BRANCH_CONTROL_DEFINITION = "86b05a0c-73e6-4f7d-b3cf-e38f3b39a75b"
REQUIRED_TEMPLATE_CHECK = "4020e66e-b0f3-47e1-bc88-48f3cc59b5f3"

# Azure
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_OWNED_BY = "Application.ReadWrite.OwnedBy"
ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_RBAC_ADMIN = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
ROLE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
LOCK_ROLE_NAME = "Sentinel Seed Lock Contributor"
# Rollen, die die Projektidentität in ihren Resource Groups vergeben darf; dieselbe Liste wie
# $AssignableRoles in onboarding/Initialize-SeedTenant.ps1 (geteilte Deployment-Identität).
SEED_ASSIGNABLE_ROLES = (
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b",  # Storage Blob Data Owner
    "974c5e8b-45b9-4653-ba55-5f855dd0fb88",  # Storage Queue Data Contributor
    "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",  # Storage Table Data Contributor
    "ba92f5b4-2d11-453d-a403-e96b0029c9fe",  # Storage Blob Data Contributor
    "4633458b-17de-408a-b874-0445c86b69e6",  # Key Vault Secrets User
    "b86a8fe4-44ce-4948-aee5-eccb2c155cd7",  # Key Vault Secrets Officer
    "3913510d-42f4-4e42-8a64-420c390055eb",  # Monitoring Metrics Publisher
)
FEDERATION_AUDIENCE = "api://AzureADTokenExchange"
NOT_FOUND = re.compile(r"ResourceNotFound|ResourceGroupNotFound|NotFound|could not be found|was not found|does not exist", re.I)
PRINCIPAL_NOT_READY = re.compile(r"PrincipalNotFound|does not exist in the directory", re.I)


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
        print(f"##vso[task.setvariable variable={name}]{clean(value)}", flush=True)


def check_auth(auth: str) -> None:
    if auth == "pat":
        fail("azureDevOpsAuth pat gibt es ab v0.20.0 nicht mehr: Ein PAT im Scaffold war für jede Person lesbar, "
             "die die Pipeline ändern kann. Das Scaffold meldet sich mit dem Job-Token an (jobToken, Standard) "
             "oder mit der Scaffold-Identität (servicePrincipal); README, Scaffold, Umstieg.")
    if auth not in AUTH_MODES:
        fail(f"azureDevOpsAuth: einer von {', '.join(AUTH_MODES)}.")


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
        token = os.environ.get("SEED_AZDO_TOKEN") or os.environ.get("SYSTEM_ACCESSTOKEN", "")
        if not org or not token or token.startswith("$("):
            fail("SYSTEM_COLLECTIONURI und SEED_AZDO_TOKEN (oder SYSTEM_ACCESSTOKEN) fehlen.")
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


def opt_in_hint(project: str) -> str:
    return (f"Ein Projektadmin von {clean(project)} nimmt die Build-Service-Identität des zentralen Projekts "
            "einmal in Project Administrators auf (Project settings, Permissions; README, Opt-in) oder führt "
            "onboarding/Grant-SeedScaffoldAccess.ps1 aus.")


def name_problems(name: str) -> list[str]:
    if not NAME_PATTERN.fullmatch(name):
        return ["name: 3-20 Zeichen, Kleinbuchstaben, Ziffern und Bindestriche, beginnt mit einem Buchstaben."]
    if name in RESERVED_NAMES:
        return [f"name: {name} ist reserviert ({', '.join(sorted(RESERVED_NAMES))})."]
    return []


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
    auth: str = "jobToken"


def input_problems(options: CheckOptions) -> list[str]:
    problems = name_problems(options.name)
    if options.mode not in MODES:
        problems.append(f"mode: einer von {', '.join(MODES)}.")
    if not PROJECT_PATTERN.fullmatch(options.ado_project) or options.ado_project.startswith(("_", ".")):
        problems.append("adoProject: Name eines Azure-DevOps-Projekts.")
    if options.mode != "createProject" and not REPOSITORY_PATTERN.fullmatch(options.repository):
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
             f"freigeschaltet. {opt_in_hint(name)}")
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


def repositories_api(ado: AzureDevOps, project_id: str) -> str:
    return f"{ado.org}/{project_id}/_apis/git/repositories"


def top_level_entries(ado: AzureDevOps, project_id: str, repo_id: str) -> list[dict]:
    """Einträge der obersten Ebene von main (Baum per API, ohne Checkout)."""
    data = ado.request("GET", f"{repositories_api(ado, project_id)}/{repo_id}/items?scopePath=%2F&recursionLevel=OneLevel"
                              f"&versionDescriptor.version=main&versionDescriptor.versionType=branch&{API}") or {}
    return [item for item in data.get("value") or [] if item.get("path") not in ("/", "")]


def unexpected_entries(items: list[dict]) -> list[str]:
    return sorted(str(item.get("path")) for item in items
                  if item.get("isFolder") or str(item.get("path", "")).lstrip("/").lower() not in INITIAL_FILES)


def scaffold_commit(ado: AzureDevOps, project_id: str, repo_id: str) -> str | None:
    """Der Commit, mit dem ein früherer Lauf seed-template eingeführt hat, oder None."""
    commits = ado.request("GET", f"{repositories_api(ado, project_id)}/{repo_id}/commits?searchCriteria.itemVersion.version=main"
                                 f"&searchCriteria.$top=100&{API}") or {}
    found = [c for c in commits.get("value") or [] if str(c.get("comment", "")).startswith(TEMPLATE_MARKER)]
    return found[-1].get("commitId") if found else None


def check_repository(ado: AzureDevOps, project: dict, repository: str, mode: str, can_create: bool = False) -> dict | None:
    repo = ado.request("GET", f"{repositories_api(ado, project['id'])}/{q(repository)}?{API}", missing_ok=True)
    if not repo:
        if mode == "new" and can_create:
            log(f"Repo {repository} gibt es noch nicht; das Scaffold legt es an (servicePrincipal).")
            return None
        hint = ("Bei new zuerst das Repo im Zielprojekt anlegen (Repos, New repository, mit README); "
                "mit dem Job-Token darf das Scaffold keine Repos anlegen.") if mode == "new" else ""
        fail(f"Repo {clean(repository)} gibt es im Projekt {clean(project['name'])} nicht. {hint}".strip())
    branch = repo.get("defaultBranch") or ""
    if not branch:
        if mode == "new" and can_create:
            log(f"Repo {repository} ist leer; das Scaffold füllt es (servicePrincipal).")
            return repo
        fail(f"Repo {clean(repository)} ist leer. Es braucht mindestens einen Commit: in Repos auf Initialize "
             "klicken (mit README), dann den Lauf wiederholen.")
    if mode == "new":
        if branch != "refs/heads/main":
            fail(f"Repo {clean(repository)}: Default-Branch ist {clean(branch)}, erwartet ist main.")
        extra = unexpected_entries(top_level_entries(ado, project["id"], repo["id"]))
        if extra:
            if not scaffold_commit(ado, project["id"], repo["id"]):
                fail(f"Repo {clean(repository)} enthält auf main schon mehr als README, LICENSE und .gitignore "
                     f"({clean(', '.join(extra[:5]))}). Ein bestehendes Projekt mit mode migrate einrichten.")
            log(f"Repo {repository}: seed-template ist schon eingeführt (früherer Lauf).")
    elif branch != "refs/heads/main":
        warn(f"Repo {clean(repository)}: Default-Branch ist {clean(branch)}. Die Branch-Prüfung an der Service "
             "Connection erlaubt dann diesen Branch; der Lauf auf main gilt nicht.")
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
    check_auth(options.auth)
    problems = input_problems(options)
    if problems:
        fail(*problems)
    if options.mode == "createProject":
        if options.allowed_projects and not re.fullmatch(options.allowed_projects, options.ado_project):
            fail(f"Projekt {clean(options.ado_project)} ist für das zentrale Scaffold nicht vorgesehen (allowedProjects).")
        return check_new_project(ado, options, central_id)
    project = find_project(ado, options.ado_project)
    if options.allowed_projects and not re.fullmatch(options.allowed_projects, project["name"]):
        fail(f"Projekt {clean(project['name'])} ist für das zentrale Scaffold nicht vorgesehen (allowedProjects).")
    group = f"[{project['name']}]\\{options.member_group}"
    if not is_member(ado, options.requester_id, group):
        fail(f"Wer den Lauf startet, muss im Zielprojekt Mitglied von {clean(group)} sein.")
    log(f"Zielprojekt {project['name']}, Mitgliedschaft in {group}: ja")
    repo = check_repository(ado, project, options.repository, options.mode,
                            can_create=options.auth == "servicePrincipal")
    problems = connection_problems(ado, central_id, project["id"], options.connections)
    if problems:
        fail(*problems)
    branch = (repo or {}).get("defaultBranch") or "refs/heads/main"
    log(f"Repo {options.repository} ({branch}), Service Connections: {', '.join(options.connections) or '-'}")
    set_variable("seedProjectId", project["id"])
    # Der Name aus der API darf in Skripte (Terraform-Werte): Azure DevOps lässt in Projektnamen
    # weder Anführungszeichen noch Steuerzeichen zu.
    set_variable("seedProjectName", project["name"])
    set_variable("seedRepositoryId", (repo or {}).get("id", ""))
    set_variable("seedRepository", options.repository)
    set_variable("seedDefaultBranch", branch.removeprefix("refs/heads/"))
    return 0


def check_new_project(ado: AzureDevOps, options: CheckOptions, central_id: str) -> int:
    """createProject: Gibt es das Projekt schon, muss die Person dort schon Mitglied sein; sonst
    nähme das Scaffold sie in ein fremdes Projekt auf."""
    problems = connection_problems(ado, central_id, central_id, options.connections)
    if problems:
        fail(*problems)
    project = ado.request("GET", f"{ado.org}/_apis/projects/{q(options.ado_project)}?{API}", missing_ok=True)
    if not project:
        log(f"Projekt {options.ado_project} gibt es noch nicht; das Scaffold legt es an.")
        set_variable("seedProjectExists", "false")
        return 0
    group = f"[{project['name']}]\\{options.member_group}"
    if not is_member(ado, options.requester_id, group):
        fail(f"Das Projekt {clean(project['name'])} gibt es schon, und wer den Lauf startet, ist dort nicht Mitglied "
             f"von {clean(group)}. Einen anderen Namen wählen oder ein Mitglied des Projekts bitten.")
    log(f"Projekt {project['name']} gibt es schon; Mitgliedschaft in {group}: ja")
    set_variable("seedProjectExists", "true")
    set_variable("seedProjectId", project["id"])
    set_variable("seedProjectName", project["name"])
    return 0


# --- create-project ----------------------------------------------------------------------------

@dataclass
class CreateProjectOptions:
    project: str
    name: str
    requester_id: str
    auth: str = "jobToken"
    process: str = "Basic"
    dry_run: bool = False
    timeout: int = 300


def graph_group(ado: AzureDevOps, project: dict, group: str) -> dict:
    scope = ado.request("GET", f"{ado.vssps}/_apis/graph/descriptors/{project['id']}?{API_PREVIEW}")["value"]
    groups = ado.request("GET", f"{ado.vssps}/_apis/graph/groups?scopeDescriptor={q(scope)}&{API_PREVIEW}") or {}
    wanted = f"[{project['name']}]\\{group}".lower()
    match = next((g for g in groups.get("value") or [] if str(g.get("principalName", "")).lower() == wanted), None)
    if not match:
        fail(f"Gruppe [{clean(project['name'])}]\\{group} nicht gefunden.")
    return match


def add_member(ado: AzureDevOps, project: dict, requester_id: str, group: str) -> None:
    container = graph_group(ado, project, group)["descriptor"]
    subject = ado.request("GET", f"{ado.vssps}/_apis/graph/descriptors/{q(requester_id)}?{API_PREVIEW}")["value"]
    url = f"{ado.vssps}/_apis/graph/memberships/{q(subject)}/{q(container)}?{API_PREVIEW}"
    if ado.request("GET", url, missing_ok=True):
        log(f"Startende Person ist schon in {group}.")
        return
    ado.request("PUT", url)
    log(f"Startende Person in {group} aufgenommen.")


def process_template(ado: AzureDevOps, name: str) -> str:
    processes = (ado.request("GET", f"{ado.org}/_apis/process/processes?{API}") or {}).get("value") or []
    match = next((p for p in processes if p.get("name") == name), None) or next((p for p in processes if p.get("isDefault")), None)
    if not match:
        fail("Keine Prozessvorlage gefunden (Basic oder Standard der Organisation).")
    return match["id"]


def wait_for_operation(ado: AzureDevOps, operation: dict, timeout: int, sleep: Callable[[float], None]) -> None:
    deadline = timeout
    while True:
        state = ado.request("GET", f"{ado.org}/_apis/operations/{operation['id']}?{API}") or {}
        status = str(state.get("status", "")).lower()
        if status == "succeeded":
            return
        if status in ("failed", "cancelled"):
            fail(f"Projekt anlegen: {status} ({clean(state.get('resultMessage') or '')})")
        if deadline <= 0:
            fail("Projekt anlegen: Die Operation ist nach fünf Minuten nicht fertig. Den Lauf später wiederholen.")
        sleep(3)
        deadline -= 3


def repository_web_url(ado: AzureDevOps, project_name: str) -> str:
    # Das Standard-Repo heißt wie das Projekt.
    return f"{ado.org}/{q(project_name)}/_git/{q(project_name)}"


def run_create_project(ado: AzureDevOps, options: CreateProjectOptions,
                       sleep: Callable[[float], None] = time.sleep) -> int:
    check_auth(options.auth)
    if not PROJECT_PATTERN.fullmatch(options.project) or options.project.startswith(("_", ".")):
        fail("adoProject: Name eines Azure-DevOps-Projekts.")
    problems = name_problems(options.name)
    if problems:
        fail(*problems)
    project = ado.request("GET", f"{ado.org}/_apis/projects/{q(options.project)}?{API}", missing_ok=True)
    if project:
        # Nur prüfen: Mitglied ist die Person schon (check); ein fremdes Projekt fasst das Scaffold nicht an.
        log(f"Projekt {project['name']} gibt es schon; nichts angelegt.")
    elif options.dry_run:
        log(f"Projekt {options.project} würde angelegt (dryRun).")
        return 0
    else:
        operation = ado.request("POST", f"{ado.org}/_apis/projects?{API}", {
            "name": options.project,
            "description": f"Sentinel Seed: {options.name}, angelegt von seed-scaffold",
            "visibility": "private",
            "capabilities": {
                "versioncontrol": {"sourceControlType": "Git"},
                "processTemplate": {"templateTypeId": process_template(ado, options.process)},
            },
        })
        wait_for_operation(ado, operation, options.timeout, sleep)
        project = ado.request("GET", f"{ado.org}/_apis/projects/{q(options.project)}?{API}")
        log(f"Projekt {project['name']} angelegt; das Scaffold ist dort Projektadministrator.")
        try:
            add_member(ado, project, options.requester_id, "Contributors")
        except ScaffoldError:
            fail(f"Projekt {clean(project['name'])} ist angelegt, aber die startende Person ließ sich nicht als "
                 "Contributor aufnehmen. Ein Projektadmin nimmt sie auf; danach den Lauf wiederholen.")
    url = repository_web_url(ado, project["name"])
    set_variable("seedProjectId", project["id"])
    set_variable("seedProjectName", project["name"])
    set_variable("seedRepository", project["name"])
    set_variable("seedRepositoryUrl", url)
    if options.auth == "jobToken":
        # Unter "Protect access to repositories" sieht das Job-Token das leere Standard-Repo nicht
        # und kann es nicht füllen (README, Weg J). Ein Klick der Person macht es sichtbar.
        warn(f"Projekt {clean(project['name'])} ist bereit. Nächster Schritt: Im Repo {clean(url)} auf Initialize "
             f"klicken (mit README), dann diese Pipeline erneut starten, mit mode new und adoProject "
             f"{clean(project['name'])}.")
    else:
        log("servicePrincipal: Der Lauf füllt das Repo gleich weiter.")
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

def run_first_run(ado: AzureDevOps, target_id: str, pipeline_id: str, authorize: tuple[str, ...] | list[str] = (),
                  start: bool = True) -> int:
    if not pipeline_id.isdigit():
        fail(f"Pipeline-ID fehlt oder ist ungültig: {clean(pipeline_id)}")
    for name in authorize:
        endpoint = endpoint_named(ado, target_id, name)
        if not endpoint:
            fail(f"Service Connection {name} fehlt im Zielprojekt.")
        ado.request("PATCH", f"{ado.org}/{target_id}/_apis/pipelines/pipelinePermissions/endpoint/{endpoint['id']}?{API_PREVIEW}",
                    {"pipelines": [{"id": int(pipeline_id), "authorized": True}]})
        log(f"Pipeline {pipeline_id} für {name} berechtigt.")
    if start:
        run = ado.request("POST", f"{ado.org}/{target_id}/_apis/pipelines/{pipeline_id}/runs?{API}", {})
        log(f"Erster Lauf: {run['_links']['web']['href']}")
    return 0


# --- fill --------------------------------------------------------------------------------------

GitRunner = Callable[[list[str], "str | None", "dict[str, str]"], "subprocess.CompletedProcess[str]"]


def git_runner(args: list[str], cwd: str | None, env: dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


@dataclass
class FillOptions:
    project_id: str
    repository: str
    template_url: str
    template_ref: str
    create_repository: bool = False
    dry_run: bool = False


def template_url_problems(url: str) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query:
        return ["templateUrl: eine https-URL ohne Anmeldedaten, etwa https://github.com/blackforestsentinel/seed-template.git."]
    return []


def overlay(source: str, target: str) -> None:
    """Dateien aus seed-template über das Repo legen; .git bleibt draußen, Links bleiben Links."""
    top = os.path.abspath(source)

    def ignore(directory: str, names: list[str]) -> set[str]:
        return {".git"} & set(names) if os.path.abspath(directory) == top else set()

    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, ignore=ignore)


def run_fill(ado: AzureDevOps, options: FillOptions, git: GitRunner = git_runner) -> int:
    if not TEMPLATE_REF_PATTERN.fullmatch(options.template_ref):
        fail("templateRef: ein Release-Tag von seed-template, etwa v0.19.0; Branches nimmt das Scaffold nicht.")
    api = repositories_api(ado, options.project_id)
    repo = ado.request("GET", f"{api}/{q(options.repository)}?{API}", missing_ok=True)
    if not repo:
        if not options.create_repository:
            fail(f"Repo {clean(options.repository)} gibt es im Zielprojekt nicht (oder das Job-Token sieht es nicht).")
        if options.dry_run:
            log(f"Repo {options.repository} würde angelegt (dryRun).")
            return 0
        repo = ado.request("POST", f"{api}?{API}", {"name": options.repository})
        log(f"Repo {options.repository} angelegt.")
    refs = ado.request("GET", f"{api}/{repo['id']}/refs?filter=heads/main&{API}") or {}
    head = next((r.get("objectId") for r in refs.get("value") or [] if r.get("name") == "refs/heads/main"), None)
    if head:
        existing = scaffold_commit(ado, options.project_id, repo["id"])
        if existing:
            log(f"seed-template ist schon eingeführt ({existing[:8]}).")
            set_variable("seedScaffoldCommit", existing)
            return 0
        extra = unexpected_entries(top_level_entries(ado, options.project_id, repo["id"]))
        if extra:
            fail(f"main enthält schon mehr als README, LICENSE und .gitignore ({clean(', '.join(extra[:5]))}). "
                 "Ein bestehendes Projekt mit mode migrate einrichten.")
    elif not options.create_repository:
        fail(f"Repo {clean(options.repository)} ist leer: in Repos auf Initialize klicken (mit README).")
    if options.dry_run:
        log(f"seed-template {options.template_ref} würde als ein Commit nach main kommen (dryRun).")
        return 0

    with tempfile.TemporaryDirectory() as work:
        # Ohne Konfiguration des Agents (global, system) und ohne Hooks: Weder seed-template noch
        # das Zielrepo bringen Code mit, der hier läuft.
        empty_config = os.path.join(work, "gitconfig")
        no_hooks = os.path.join(work, "no-hooks")
        os.makedirs(no_hooks)
        open(empty_config, "w", encoding="utf-8").close()
        base_env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": empty_config}
        # Das Token geht nur an Azure DevOps, über die Umgebung statt als Argument (ps).
        ado_env = {**base_env, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                   "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {ado.token}"}
        settings = ["-c", f"core.hooksPath={no_hooks}", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                    "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=user"]

        def run(args: list[str], cwd: str | None, env: dict[str, str], what: str) -> str:
            result = git([*settings, *args], cwd, env)
            if result.returncode:
                fail(f"{what}: {clean(result.stderr.strip())}")
            return result.stdout.strip()

        template = os.path.join(work, "template")
        run(["clone", "--quiet", "--depth", "1", "--branch", options.template_ref, "--", options.template_url, template],
            None, base_env, f"seed-template {options.template_ref} nicht lesbar")
        template_commit = run(["rev-parse", "HEAD"], template, base_env, "seed-template")
        target = os.path.join(work, "target")
        if head:
            run(["clone", "--quiet", "--branch", "main", "--single-branch", "--", repo["remoteUrl"], target],
                None, ado_env, "Zielrepo nicht lesbar")
            current = run(["rev-parse", "HEAD"], target, base_env, "Zielrepo")
            if current != head:
                fail("main hat sich während des Laufs verändert; den Lauf wiederholen.")
        else:
            os.makedirs(target)
            run(["init", "--quiet", "--initial-branch", "main"], target, base_env, "Zielrepo")
            run(["remote", "add", "origin", repo["remoteUrl"]], target, base_env, "Zielrepo")
        overlay(template, target)
        run(["add", "--all"], target, base_env, "Zielrepo")
        run(["-c", "user.name=Sentinel Seed Scaffold", "-c", "user.email=seed-scaffold@users.noreply.dev.azure.com",
             "commit", "--quiet", "-m", f"{TEMPLATE_MARKER} {options.template_ref} eingeführt",
             "-m", f"Quelle: {options.template_url} {options.template_ref} ({template_commit})"],
            target, base_env, "Commit")
        commit = run(["rev-parse", "HEAD"], target, base_env, "Zielrepo")
        # Kein Force-Push: Hat jemand main inzwischen bewegt, lehnt Azure DevOps ab.
        run(["push", "--quiet", "origin", "HEAD:refs/heads/main"], target, ado_env,
            "Push nach main abgelehnt (main inzwischen verändert?)")
    log(f"seed-template {options.template_ref} eingeführt: {commit[:8]}")
    set_variable("seedScaffoldCommit", commit)
    return 0


# --- verify ------------------------------------------------------------------------------------

def run_verify(ado: AzureDevOps, project_id: str, repository: str, commit: str, limit: int = 20) -> int:
    """main darf seit dem Scaffold-Commit nur Pushes des Scaffolds enthalten, und deren Commits nur
    project.yaml und azure-pipelines.yml ändern (Terraform). Sonst bekäme die Pipeline gleich
    die Service Connection für Inhalt, den niemand geprüft hat."""
    if not COMMIT_PATTERN.fullmatch(commit):
        fail(f"Scaffold-Commit fehlt oder ist ungültig: {clean(commit)}")
    api = repositories_api(ado, project_id)
    repo = ado.request("GET", f"{api}/{q(repository)}?{API}")
    refs = ado.request("GET", f"{api}/{repo['id']}/refs?filter=heads/main&{API}") or {}
    head = next((r.get("objectId") for r in refs.get("value") or [] if r.get("name") == "refs/heads/main"), None)
    if not head:
        fail("main fehlt im Zielrepo.")
    me = (ado.request("GET", f"{ado.org}/_apis/connectionData?{API_PREVIEW}") or {}).get("authenticatedUser", {}).get("id")
    if not me:
        fail("Eigene Identität nicht lesbar (connectionData).")
    pushes = (ado.request("GET", f"{api}/{repo['id']}/pushes?searchCriteria.refName=refs%2Fheads%2Fmain"
                                 f"&searchCriteria.includeRefUpdates=true&$top=100&{API}") or {}).get("value") or []

    # Kette der Pushes von main rückwärts bis zu dem, der den Scaffold-Commit gebracht hat.
    by_new = {}
    for push in pushes:
        for update in push.get("refUpdates") or []:
            if update.get("name", "refs/heads/main") == "refs/heads/main":
                by_new.setdefault(update.get("newObjectId"), (push, update))
    current = head
    for _ in range(limit):
        if current not in by_new:
            fail("main: Der Weg vom Scaffold-Commit zum aktuellen Stand ist nicht nachvollziehbar (fremder Push oder "
                 "zu viele Pushes). Die Pipeline wird nicht berechtigt; README, Scaffold, verify.")
        push, update = by_new[current]
        pusher = (push.get("pushedBy") or {}).get("id")
        if pusher != me:
            fail(f"main: Push {push.get('pushId')} kam nicht vom Scaffold ({clean((push.get('pushedBy') or {}).get('displayName', '?'))}). "
                 "Die Pipeline wird nicht berechtigt; README, Scaffold, verify.")
        if current == commit:
            break
        current = update.get("oldObjectId")
    else:
        fail("main: Scaffold-Commit nicht unter den letzten Pushes gefunden.")

    # Inhalt: jeder Commit nach dem Scaffold-Commit einzeln, ohne Merges, nur Dateien von Terraform.
    current, steps = head, 0
    while current != commit:
        steps += 1
        if steps > limit:
            fail("main: Scaffold-Commit nicht in den letzten Commits gefunden.")
        detail = ado.request("GET", f"{api}/{repo['id']}/commits/{current}?{API}") or {}
        parents = detail.get("parents") or []
        if len(parents) != 1:
            fail(f"main: Commit {current[:8]} hat {len(parents)} Eltern; erwartet sind nur Commits des Scaffolds.")
        changes = (ado.request("GET", f"{api}/{repo['id']}/commits/{current}/changes?{API}") or {}).get("changes") or []
        paths = sorted({c.get("item", {}).get("path", "") for c in changes if not c.get("item", {}).get("isFolder")})
        foreign = [p for p in paths if p not in TERRAFORM_FILES]
        if foreign:
            fail(f"main: Commit {current[:8]} ändert {clean(', '.join(foreign[:5]))}; erwartet sind nur "
                 f"{', '.join(TERRAFORM_FILES)} von Terraform.")
        current = parents[0]
    log(f"main: Scaffold-Commit {commit[:8]} und {steps} Commit(s) von Terraform, keine fremden Pushes.")
    return 0


# --- connection (migrate) ----------------------------------------------------------------------

@dataclass
class ConnectionOptions:
    project_id: str
    name: str
    tenant_id: str
    subscription_id: str
    subscription_name: str
    client_id: str
    allowed_branches: list[str]
    pipelines_version: str
    dry_run: bool = False


def endpoint_body(project: dict, connection: str, options: ConnectionOptions) -> dict:
    return {
        "name": connection,
        "type": "azurerm",
        "url": "https://management.azure.com/",
        "description": f"Sentinel Seed: Projektidentität id-{options.name} (Workload Identity Federation)",
        "authorization": {"scheme": "WorkloadIdentityFederation",
                          "parameters": {"tenantid": options.tenant_id, "serviceprincipalid": options.client_id}},
        "data": {"environment": "AzureCloud", "scopeLevel": "Subscription", "subscriptionId": options.subscription_id,
                 "subscriptionName": options.subscription_name, "creationMode": "Manual"},
        "isShared": False,
        "isReady": True,
        "serviceEndpointProjectReferences": [{"projectReference": {"id": project["id"], "name": project["name"]},
                                              "name": connection}],
    }


def template_entry(ref: str) -> dict:
    return {"repositoryType": "github", "repositoryName": SEED_PIPELINES_REPOSITORY,
            "repositoryRef": f"refs/tags/{ref}", "templatePath": WEB_APP_TEMPLATE}


def run_connection(ado: AzureDevOps, options: ConnectionOptions) -> int:
    problems = name_problems(options.name)
    if not TEMPLATE_REF_PATTERN.fullmatch(options.pipelines_version):
        problems.append("pipelinesVersion: ein Release-Tag von seed-pipelines, etwa v0.20.0.")
    if not options.client_id or not options.tenant_id or not options.subscription_id:
        problems.append("Client-ID, Tenant und Subscription der Projektidentität fehlen (Schritt identity).")
    if problems:
        fail(*problems)
    connection = f"sc-{options.name}"
    project = ado.request("GET", f"{ado.org}/_apis/projects/{q(options.project_id)}?{API}")
    endpoint = endpoint_named(ado, project["id"], connection)
    if endpoint:
        principal = ((endpoint.get("authorization") or {}).get("parameters") or {}).get("serviceprincipalid", "")
        if principal.lower() != options.client_id.lower():
            fail(f"Service Connection {connection} gibt es schon, aber nicht für id-{options.name}. Sie umbenennen "
                 "oder löschen und den Lauf wiederholen.")
        log(f"Service Connection {connection}: vorhanden")
    elif options.dry_run:
        log(f"Service Connection {connection} würde angelegt (dryRun).")
        return 0
    else:
        ado.request("POST", f"{ado.org}/_apis/serviceendpoint/endpoints?{API}", endpoint_body(project, connection, options))
        endpoint = endpoint_named(ado, project["id"], connection)
        if not endpoint:
            fail(f"Service Connection {connection} ist nach dem Anlegen nicht zu sehen.")
        log(f"Service Connection {connection}: angelegt")
    parameters = (endpoint.get("authorization") or {}).get("parameters") or {}
    set_variable("seedServiceConnectionId", endpoint["id"])
    set_variable("seedFederationIssuer", parameters.get("workloadIdentityFederationIssuer", ""))
    set_variable("seedFederationSubject", parameters.get("workloadIdentityFederationSubject", ""))

    checks_url = f"{ado.org}/{project['id']}/_apis/pipelines/checks/configurations"
    existing = (ado.request("GET", f"{checks_url}?resourceType=endpoint&resourceId={endpoint['id']}&$expand=settings&{API_PREVIEW}")
                or {}).get("value") or []
    resource = {"type": "endpoint", "id": endpoint["id"], "name": connection}
    branches = ",".join(options.allowed_branches)
    branch_check = next((c for c in existing if (c.get("type") or {}).get("id") == BRANCH_CONTROL_CHECK
                         and ((c.get("settings") or {}).get("definitionRef") or {}).get("id") == BRANCH_CONTROL_DEFINITION), None)
    if branch_check:
        log(f"Branch-Prüfung an {connection}: {clean((branch_check['settings'].get('inputs') or {}).get('allowedBranches', ''))}")
    elif options.dry_run:
        log(f"Branch-Prüfung an {connection} würde angelegt: {branches} (dryRun)")
    else:
        ado.request("POST", f"{checks_url}?{API_PREVIEW}", {
            "type": {"id": BRANCH_CONTROL_CHECK, "name": "Task Check"},
            "resource": resource,
            "settings": {
                "definitionRef": {"id": BRANCH_CONTROL_DEFINITION, "name": "evaluatebranchProtection", "version": "0.0.1"},
                "displayName": "Sentinel Seed: nur freigegebene Branches",
                "inputs": {"allowedBranches": branches, "ensureProtectionOfBranch": "false", "allowUnknownStatusBranch": "false"},
            },
            "timeout": 1440,
        })
        log(f"Branch-Prüfung an {connection}: {branches}")
    wanted = template_entry(options.pipelines_version)
    template_check = next((c for c in existing if (c.get("type") or {}).get("id") == REQUIRED_TEMPLATE_CHECK), None)
    entries = list(((template_check or {}).get("settings") or {}).get("extendsChecks") or [])
    if any(all(str(e.get(k, "")).lower() == str(v).lower() for k, v in wanted.items()) for e in entries):
        log(f"Required template an {connection}: {WEB_APP_TEMPLATE}@{options.pipelines_version} vorhanden")
    elif options.dry_run:
        log(f"Required template an {connection} würde ergänzt: {WEB_APP_TEMPLATE}@{options.pipelines_version} (dryRun)")
    else:
        body = {"type": {"id": REQUIRED_TEMPLATE_CHECK, "name": "ExtendsCheck"}, "resource": resource,
                "settings": {"extendsChecks": [*entries, wanted]}, "timeout": 1440}
        if template_check:
            body["id"] = template_check["id"]
            ado.request("PATCH", f"{checks_url}/{template_check['id']}?{API_PREVIEW}", body)
        else:
            ado.request("POST", f"{checks_url}?{API_PREVIEW}", body)
        log(f"Required template an {connection}: {WEB_APP_TEMPLATE}@{options.pipelines_version}")
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
    problems = name_problems(options.name)
    if problems:
        fail(*problems)
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


# --- identity ----------------------------------------------------------------------------------

def rbac_condition(role_ids: tuple[str, ...] | list[str]) -> str:
    """Role Based Access Control Administrator nur für diese Rollen, wie im Tenant-Onboarding."""
    ids = ", ".join(role_ids)
    return ("((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR "
            f"(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {{{ids}}})) AND "
            "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR "
            f"(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {{{ids}}}))")


def blob_condition(prefixes: list[str]) -> str:
    """Storage Blob Data Contributor nur für Blobs unter diesen Pfaden; auflisten darf die Identität
    den ganzen Container (eine Liste nennt nur Namen), wie die Scaffold-Identität für scaffold/."""
    blobs = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"
    paths = " OR ".join(f"(@Resource[{blobs}:path] StringStartsWith '{p}')" for p in prefixes)
    return (f"(((!(ActionMatches{{'{blobs}/read'}} AND NOT SubOperationMatches{{'Blob.List'}})) AND "
            f"(!(ActionMatches{{'{blobs}/write'}})) AND (!(ActionMatches{{'{blobs}/add/action'}})) AND "
            f"(!(ActionMatches{{'{blobs}/delete'}}))) OR {paths})")


def state_prefixes(name: str) -> list[str]:
    # State unter <name>/<env>.tfstate, Pläne unter plans/<name>/<env>/ (web-app.yml).
    return [f"{name}/", f"plans/{name}/"]


class Az:
    def __init__(self, runner: Runner, sleep: Callable[[float], None] = time.sleep) -> None:
        self.runner, self.sleep = runner, sleep

    def json(self, *args: str, missing_ok: bool = False):
        result = self.runner([*args, "-o", "json"])
        if result.returncode:
            if missing_ok and NOT_FOUND.search(result.stderr):
                return None
            fail(f"az {' '.join(args[:2])}: {clean(result.stderr.strip())}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def run(self, *args: str, retry_principal: bool = False) -> None:
        for attempt in range(1, 13):
            result = self.runner([*args, "-o", "none"])
            if result.returncode == 0:
                return
            # Eine neue Managed Identity kennt die Rollenverwaltung oft erst nach einigen Sekunden.
            if retry_principal and attempt < 12 and PRINCIPAL_NOT_READY.search(result.stderr):
                self.sleep(10)
                continue
            fail(f"az {' '.join(args[:3])}: {clean(result.stderr.strip())}")


@dataclass
class IdentityOptions:
    name: str
    environments: list[str]
    location: str
    identity_resource_group: str
    state_resource_group: str
    state_account: str
    container: str
    sso: bool = False
    dry_run: bool = False
    owner: str = ""


@dataclass
class RoleWanted:
    scope: str
    role_id: str
    title: str
    condition: str | None = None


def ensure_role(az: Az, principal_id: str, wanted: RoleWanted, dry_run: bool) -> None:
    assignments = az.json("role", "assignment", "list", "--scope", wanted.scope, "--role", wanted.role_id) or []
    mine = [a for a in assignments if a.get("principalId") == principal_id
            and str(a.get("scope", "")).lower() == wanted.scope.lower()]
    if mine and (mine[0].get("condition") or None) == wanted.condition:
        log(f"  [ok]    {wanted.title}")
        return
    if dry_run:
        log(f"  [fehlt] {wanted.title}{' (Bedingung)' if mine else ''}")
        return
    if mine:
        # Neue Seed-Versionen erlauben weitere Rollen: die Bedingung ersetzen, nicht ergänzen.
        updated = {**mine[0], "condition": wanted.condition, "conditionVersion": "2.0" if wanted.condition else None}
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "assignment.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(updated, handle)
            az.run("role", "assignment", "update", "--role-assignment", f"@{path}")
        log(f"  [neu]   {wanted.title}: Bedingung aktualisiert")
        return
    args = ["role", "assignment", "create", "--assignee-object-id", principal_id, "--assignee-principal-type",
            "ServicePrincipal", "--role", wanted.role_id, "--scope", wanted.scope,
            "--description", f"Sentinel Seed: {wanted.title}"]
    if wanted.condition:
        args += ["--condition", wanted.condition, "--condition-version", "2.0"]
    az.run(*args, retry_principal=True)
    log(f"  [neu]   {wanted.title}")


def ensure_graph_role(az: Az, principal_id: str, dry_run: bool) -> None:
    graph = az.json("ad", "sp", "show", "--id", GRAPH_APP_ID)
    role = next((r for r in graph.get("appRoles") or [] if r.get("value") == GRAPH_OWNED_BY), None)
    if not role:
        fail(f"Graph-Rolle {GRAPH_OWNED_BY} nicht gefunden.")
    url = f"https://graph.microsoft.com/v1.0/servicePrincipals/{principal_id}/appRoleAssignments"
    assigned = (az.json("rest", "--method", "get", "--url", url) or {}).get("value") or []
    if any(a.get("appRoleId") == role["id"] and a.get("resourceId") == graph["id"] for a in assigned):
        log(f"  [ok]    Graph {GRAPH_OWNED_BY}")
        return
    if dry_run:
        log(f"  [fehlt] Graph {GRAPH_OWNED_BY}")
        return
    body = json.dumps({"principalId": principal_id, "resourceId": graph["id"], "appRoleId": role["id"]})
    az.run("rest", "--method", "post", "--url", url, "--headers", "Content-Type=application/json", "--body", body,
           retry_principal=True)
    log(f"  [neu]   Graph {GRAPH_OWNED_BY}")


def run_identity(options: IdentityOptions, runner: Runner = az_runner, sleep: Callable[[float], None] = time.sleep) -> int:
    problems = name_problems(options.name)
    if options.owner and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", options.owner):
        problems.append("owner: eine E-Mail-Adresse, z. B. team@example.org, oder leer.")
    if not options.environments or not all(ENVIRONMENT_PATTERN.fullmatch(e) for e in options.environments):
        problems.append("environments: mindestens eine Umgebung, Kleinbuchstaben, Ziffern und Bindestriche, höchstens 20 Zeichen.")
    if problems:
        fail(*problems)
    az = Az(runner, sleep)
    account = az.json("account", "show")
    subscription = f"/subscriptions/{account['id']}"
    identity_name = f"id-{options.name}"
    log(f"Identität {identity_name} in {options.identity_resource_group}{' (dryRun)' if options.dry_run else ''}")

    identity = az.json("identity", "show", "--name", identity_name, "--resource-group", options.identity_resource_group,
                       missing_ok=True)
    if identity:
        owner = (identity.get("tags") or {}).get("seed:project")
        if owner != options.name:
            fail(f"{identity_name} gibt es schon, aber nicht für das Projekt {options.name} (Tag seed:project).")
        log(f"  [ok]    Managed Identity {identity_name}")
    elif options.dry_run:
        log(f"  [fehlt] Managed Identity {identity_name}")
    else:
        # Tags getrennt: az identity create (Azure CLI ab 2.9x, Shorthand-Syntax) nimmt Tag-Namen mit
        # Doppelpunkt nicht mehr an; az resource tag versteht sie weiter.
        identity = az.json("identity", "create", "--name", identity_name, "--resource-group", options.identity_resource_group,
                           "--location", options.location)
        az.run("resource", "tag", "--ids", identity["id"], "--tags", f"seed:project={options.name}",
               "seed:purpose=project-identity")
        identity["tags"] = {"seed:project": options.name, "seed:purpose": "project-identity"}
        log(f"  [neu]   Managed Identity {identity_name}")

    groups = []
    for env in options.environments:
        group_name = f"rg-{options.name}-{env}"
        group = az.json("group", "show", "--name", group_name, missing_ok=True)
        if group:
            if (group.get("tags") or {}).get("seed:project") != options.name:
                fail(f"Resource Group {group_name} gibt es schon, aber nicht für das Projekt {options.name} (Tag seed:project).")
            log(f"  [ok]    Resource Group {group_name}")
        elif options.dry_run:
            log(f"  [fehlt] Resource Group {group_name}")
            group = {"id": f"{subscription}/resourceGroups/{group_name}", "name": group_name}
        else:
            # Tags wie im Modul core (seed-terraform); mit create_resource_group = false setzt core keine.
            tags = [f"seed:project={options.name}", f"seed:environment={env}"]
            if options.owner:
                tags.append(f"owner={options.owner}")
            group = az.json("group", "create", "--name", group_name, "--location", options.location, "--tags", *tags)
            log(f"  [neu]   Resource Group {group_name}")
        groups.append(group)

    if not identity:
        log("  Rollen, State und Graph folgen, sobald es die Identität gibt.")
        return 0
    principal = identity["principalId"]
    lock_roles = az.json("role", "definition", "list", "--name", LOCK_ROLE_NAME, "--custom-role-only", "true") or []
    if not lock_roles:
        fail(f"Rolle {LOCK_ROLE_NAME} fehlt: zuerst onboarding/Initialize-SeedTenant.ps1.")
    lock_role = lock_roles[0]["name"]
    wanted = []
    for group in groups:
        wanted += [
            RoleWanted(group["id"], ROLE_CONTRIBUTOR, f"Contributor auf {group['name']}"),
            RoleWanted(group["id"], ROLE_RBAC_ADMIN, f"Role Based Access Control Administrator (nur Seed-Rollen) auf {group['name']}",
                       rbac_condition(SEED_ASSIGNABLE_ROLES)),
            RoleWanted(group["id"], lock_role, f"{LOCK_ROLE_NAME} auf {group['name']}"),
        ]
    container = (f"{subscription}/resourceGroups/{options.state_resource_group}/providers/Microsoft.Storage/storageAccounts/"
                 f"{options.state_account}/blobServices/default/containers/{options.container}")
    prefixes = state_prefixes(options.name)
    wanted.append(RoleWanted(container, ROLE_BLOB_DATA_CONTRIBUTOR,
                             f"Storage Blob Data Contributor auf {options.container} (nur {', '.join(prefixes)})",
                             blob_condition(prefixes)))
    for role in wanted:
        ensure_role(az, principal, role, options.dry_run)
    if options.sso:
        ensure_graph_role(az, principal, options.dry_run)

    set_variable("seedIdentityClientId", identity["clientId"])
    set_variable("seedIdentityPrincipalId", principal)
    set_variable("seedTenantId", account["tenantId"])
    set_variable("seedSubscriptionId", account["id"])
    set_variable("seedSubscriptionName", account["name"])
    return 0


@dataclass
class FederateOptions:
    name: str
    identity_resource_group: str
    issuer: str
    subject: str
    dry_run: bool = False


def run_federate(options: FederateOptions, runner: Runner = az_runner) -> int:
    problems = name_problems(options.name)
    if not options.issuer.startswith("https://") or re.search(r"\s", options.issuer):
        problems.append("issuer: https-URL aus der Service Connection (Terraform-Ausgabe workload_identity_federation_issuer).")
    if not options.subject or re.search(r"[\s\x00-\x1f]", options.subject) or len(options.subject) > 600:
        problems.append("subject: aus der Service Connection (Terraform-Ausgabe workload_identity_federation_subject).")
    if problems:
        fail(*problems)
    az = Az(runner)
    identity_name = f"id-{options.name}"
    credential = f"ado-sc-{options.name}"
    existing = az.json("identity", "federated-credential", "list", "--identity-name", identity_name,
                       "--resource-group", options.identity_resource_group) or []
    if any(c.get("subject") == options.subject and c.get("issuer") == options.issuer for c in existing):
        log(f"  [ok]    Federated Credential an {identity_name}")
        return 0
    named = any(c.get("name") == credential for c in existing)
    if options.dry_run:
        log(f"  [fehlt] Federated Credential {credential} an {identity_name}")
        return 0
    az.run("identity", "federated-credential", "update" if named else "create", "--name", credential,
           "--identity-name", identity_name, "--resource-group", options.identity_resource_group,
           "--issuer", options.issuer, "--subject", options.subject, "--audiences", FEDERATION_AUDIENCE)
    log(f"  [neu]   Federated Credential {credential} an {identity_name}")
    return 0


# --- Aufruf ------------------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    auth_default = os.environ.get("SEED_AZDO_AUTH", "jobToken")

    check = sub.add_parser("check", help="Eingaben, Zielprojekt, Mitgliedschaft, Repo, Service Connections")
    check.add_argument("--name", required=True)
    check.add_argument("--mode", required=True)
    check.add_argument("--ado-project", required=True)
    check.add_argument("--repository", default="")
    check.add_argument("--allowed-projects", default="", help="Regulärer Ausdruck für erlaubte Zielprojekte")
    check.add_argument("--member-group", default="Contributors", help="Gruppe im Zielprojekt, Standard Contributors")
    check.add_argument("--requester-id", default=os.environ.get("BUILD_REQUESTEDFORID", ""))
    check.add_argument("--connection", action="append", default=[], help="Name einer Service Connection (mehrfach)")
    check.add_argument("--auth", default=auth_default)

    create = sub.add_parser("create-project", help="Azure-DevOps-Projekt anlegen (createProject)")
    create.add_argument("--ado-project", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--requester-id", default=os.environ.get("BUILD_REQUESTEDFORID", ""))
    create.add_argument("--process", default="Basic")
    create.add_argument("--auth", default=auth_default)
    create.add_argument("--dry-run", action="store_true")

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

    identity = sub.add_parser("identity", help="Projektidentität in Azure (identity) oder ihr Federated Credential (identity federate)")
    identity.add_argument("action", nargs="?", default="create", choices=("create", "federate"))
    identity.add_argument("--name", required=True)
    identity.add_argument("--identity-resource-group", default="rg-seed-identities")
    identity.add_argument("--environments-json", default="[]")
    identity.add_argument("--location", default="westeurope")
    identity.add_argument("--state-resource-group", default="")
    identity.add_argument("--storage-account", default="")
    identity.add_argument("--container", default="")
    identity.add_argument("--sso", default="false")
    identity.add_argument("--owner", default="", help="E-Mail für den Tag owner der Resource Groups")
    identity.add_argument("--issuer", default="")
    identity.add_argument("--subject", default="")
    identity.add_argument("--dry-run", action="store_true")

    fill = sub.add_parser("fill", help="seed-template am Tag als einen Commit nach main")
    fill.add_argument("--project-id", required=True)
    fill.add_argument("--repository", required=True)
    fill.add_argument("--template-url", required=True)
    fill.add_argument("--template-ref", required=True)
    fill.add_argument("--auth", default=auth_default)
    fill.add_argument("--dry-run", action="store_true")

    connection = sub.add_parser("connection", help="Service Connection sc-<name> mit Prüfungen (migrate)")
    connection.add_argument("--project-id", required=True)
    connection.add_argument("--name", required=True)
    connection.add_argument("--tenant-id", default="")
    connection.add_argument("--subscription-id", default="")
    connection.add_argument("--subscription-name", default="")
    connection.add_argument("--client-id", default="")
    connection.add_argument("--default-branch", default="main")
    connection.add_argument("--pipelines-version", required=True)
    connection.add_argument("--dry-run", action="store_true")

    verify = sub.add_parser("verify", help="main enthält nur Scaffold- und Terraform-Commits")
    verify.add_argument("--project-id", required=True)
    verify.add_argument("--repository", required=True)
    verify.add_argument("--commit", required=True)

    share = sub.add_parser("share", help="GitHub-Service-Connection ins Zielprojekt teilen")
    share.add_argument("--project-id", required=True)
    share.add_argument("--connection", action="append", default=[])

    run = sub.add_parser("run", help="Pipeline berechtigen und ersten Lauf im Zielprojekt starten")
    run.add_argument("--project-id", required=True)
    run.add_argument("--pipeline-id", required=True)
    run.add_argument("--authorize", action="append", default=[], help="Service Connection im Zielprojekt (mehrfach)")
    run.add_argument("--no-start", action="store_true")
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
         runner: Runner = az_runner, git: GitRunner = git_runner) -> int:
    args = parse_args(argv)
    if args.command == "reserve":
        return run_reserve(ReserveOptions(
            args.name, args.mode, args.storage_account, args.container, args.project_id, args.project_name,
            args.repository, environments_from_json(args.environments_json), args.requested_by, args.build_id,
            args.dry_run), runner)
    if args.command == "identity":
        if args.action == "federate":
            return run_federate(FederateOptions(args.name, args.identity_resource_group, args.issuer, args.subject,
                                                args.dry_run), runner)
        return run_identity(IdentityOptions(
            args.name, environments_from_json(args.environments_json), args.location, args.identity_resource_group,
            args.state_resource_group, args.storage_account, args.container, args.sso.lower() == "true",
            args.dry_run, args.owner), runner)
    check_auth(getattr(args, "auth", "jobToken"))
    if args.command == "fill":
        problems = template_url_problems(args.template_url)
        if problems:
            fail(*problems)
    ado = ado_factory()
    if args.command == "create-project":
        return run_create_project(ado, CreateProjectOptions(args.ado_project, args.name, args.requester_id, args.auth,
                                                            args.process, args.dry_run))
    if args.command == "fill":
        return run_fill(ado, FillOptions(args.project_id, args.repository, args.template_url, args.template_ref,
                                         create_repository=args.auth == "servicePrincipal", dry_run=args.dry_run), git)
    if args.command == "verify":
        return run_verify(ado, args.project_id, args.repository, args.commit)
    if args.command == "connection":
        branch = args.default_branch if args.default_branch.startswith("refs/") else f"refs/heads/{args.default_branch}"
        return run_connection(ado, ConnectionOptions(
            args.project_id, args.name, args.tenant_id, args.subscription_id, args.subscription_name, args.client_id,
            [branch, "refs/tags/v*"], args.pipelines_version, args.dry_run))
    if args.command == "run":
        return run_first_run(ado, args.project_id, args.pipeline_id, args.authorize, start=not args.no_start)
    central_id = os.environ.get("SYSTEM_TEAMPROJECTID", "")
    if not central_id:
        fail("SYSTEM_TEAMPROJECTID fehlt.")
    if args.command == "check":
        return run_check(ado, CheckOptions(args.name, args.mode, args.ado_project, args.repository,
                                           args.allowed_projects, args.member_group, args.requester_id,
                                           args.connection, args.auth), central_id)
    return run_share(ado, central_id, args.project_id, args.connection)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ScaffoldError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)
