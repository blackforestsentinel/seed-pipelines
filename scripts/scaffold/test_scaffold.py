"""Tests für scaffold.py, nur Standardbibliothek.

Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Laden über den Pfad: Bei der Suche ab scripts/ heißt das Paket dieses Ordners schon „scaffold“.
_spec = importlib.util.spec_from_file_location("seed_scaffold", Path(__file__).with_name("scaffold.py"))
scaffold = importlib.util.module_from_spec(_spec)
sys.modules["seed_scaffold"] = scaffold
_spec.loader.exec_module(scaffold)

# Fehler sind hier gewollt; ohne TF_BUILD erscheinen sie nicht als Annotation im Lauf.
os.environ.pop("TF_BUILD", None)

ORG = "https://dev.azure.com/org"
VSSPS = "https://vssps.dev.azure.com/org"
CENTRAL = "c0000000-0000-0000-0000-000000000000"
TARGET = "t0000000-0000-0000-0000-000000000000"
PERSON = "p0000000-0000-0000-0000-000000000000"
CONTRIBUTORS = {"identityType": "Microsoft.TeamFoundation.Identity", "identifier": "S-1-9-contributors"}
OTHER_GROUP = {"identityType": "Microsoft.TeamFoundation.Identity", "identifier": "S-1-9-other"}


class FakeAzureDevOps(scaffold.AzureDevOps):
    """Antworten je (Methode, URL ohne Abfrage-Teil); fehlende GETs gelten als nicht vorhanden."""

    def __init__(self, routes: dict) -> None:
        super().__init__(ORG, "token")
        self.routes = routes
        self.calls = []

    def request(self, method, url, body=None, missing_ok=False):
        self.calls.append((method, url, body))
        path = url.split("?")[0]
        for (m, prefix), answer in self.routes.items():
            if m == method and (path == prefix or (prefix.endswith("*") and path.startswith(prefix[:-1]))):
                result = answer(url, body) if callable(answer) else answer
                if result is None and not missing_ok:
                    scaffold.fail(f"404 {path}")
                return result
        if missing_ok:
            return None
        scaffold.fail(f"unerwartet: {method} {path}")


def endpoints(*items):
    return lambda url, body: {"value": [e for e in items if f"endpointNames={e['name']}" in url]}


def tree(*paths, folders=()):
    return {"value": [{"path": "/", "isFolder": True}] + [{"path": p} for p in paths]
            + [{"path": f, "isFolder": True} for f in folders]}


def base_routes(member_of=(CONTRIBUTORS,), repo=None, target_endpoints=(), items=None, commits=()):
    repo = repo if repo is not None else {"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/main"}
    items = items if items is not None else tree("/README.md")
    central_endpoints = ({"id": "e-azure", "name": "sc-seed"}, {"id": "e-github", "name": "github-example"})
    return {
        ("GET", f"{ORG}/_apis/projects/Projekt-0042"): {"id": TARGET, "name": "Projekt-0042"},
        ("GET", f"{ORG}/_apis/projects/{TARGET}"): {"id": TARGET, "name": "Projekt-0042"},
        ("GET", f"{VSSPS}/_apis/identities"): lambda url, body: (
            {"value": [{"descriptor": "user", "memberOf": list(member_of), "providerDisplayName": "Erika"}]}
            if "identityIds=" in url else
            {"value": [{"isContainer": True, "providerDisplayName": "[Projekt-0042]\\Contributors", "descriptor": CONTRIBUTORS}]}),
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/kundenportal"): repo or None,
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/r1/items"): items,
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/r1/commits"): {"value": list(commits)},
        ("GET", f"{ORG}/{CENTRAL}/_apis/serviceendpoint/endpoints"): endpoints(*central_endpoints),
        ("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints"): endpoints(*target_endpoints),
    }


def options(**overrides):
    values = dict(name="kundenportal", mode="new", ado_project="Projekt-0042", repository="kundenportal",
                  allowed_projects=r"Projekt-\d{4}", member_group="Contributors", requester_id=PERSON,
                  connections=["github-example"])
    values.update(overrides)
    return scaffold.CheckOptions(**values)


def quiet(function, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = function(*args)
    return result, out.getvalue()


class CheckTest(unittest.TestCase):
    def check(self, routes, **overrides):
        return quiet(scaffold.run_check, FakeAzureDevOps(routes), options(**overrides), CENTRAL)

    def test_alles_in_ordnung(self):
        result, out = self.check(base_routes())
        self.assertEqual(result, 0)
        self.assertIn("Mitgliedschaft in [Projekt-0042]\\Contributors: ja", out)

    def test_eingaben_werden_vor_jedem_aufruf_geprueft(self):
        fake = FakeAzureDevOps({})
        with self.assertRaises(scaffold.ScaffoldError) as error:
            scaffold.run_check(fake, options(name='x"; echo INJIZIERT; "', repository="a b", mode="neu"), CENTRAL)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(str(error.exception).splitlines()), 3)

    def test_projektname_ohne_steuerzeichen(self):
        with self.assertRaises(scaffold.ScaffoldError):
            self.check(base_routes(), ado_project="Projekt-0042\n##vso[task.setvariable variable=x]1")

    def test_projekt_ausserhalb_des_musters(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht vorgesehen"):
            self.check(base_routes(), allowed_projects=r"X\d{4}")

    def test_nicht_freigeschaltet(self):
        routes = base_routes()
        routes[("GET", f"{ORG}/_apis/projects/Projekt-0042")] = None
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Grant-SeedScaffoldAccess"):
            self.check(routes)

    def test_keine_mitgliedschaft(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Mitglied von"):
            self.check(base_routes(member_of=(OTHER_GROUP,)))

    def test_repo_fehlt(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "New repository"):
            self.check(base_routes(repo={}))

    def test_leeres_repo(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "leer.*Initialize"):
            self.check(base_routes(repo={"id": "r1", "name": "kundenportal"}))

    def test_service_principal_darf_fehlendes_und_leeres_repo_fuellen(self):
        for repo in ({}, {"id": "r1", "name": "kundenportal"}):
            with self.subTest(repo=repo):
                result, out = self.check(base_routes(repo=repo), auth="servicePrincipal")
                self.assertEqual(result, 0)
                self.assertIn("servicePrincipal", out)

    def test_pat_modus_bricht_mit_hinweis_ab(self):
        fake = FakeAzureDevOps({})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "ab v0.20.0 nicht mehr"):
            scaffold.run_check(fake, options(auth="pat"), CENTRAL)
        self.assertEqual(fake.calls, [])

    def test_reservierte_namen(self):
        for name in ("plans", "scaffold"):
            with self.subTest(name=name), self.assertRaisesRegex(scaffold.ScaffoldError, "reserviert"):
                self.check(base_routes(), name=name)

    def test_new_braucht_main(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "erwartet ist main"):
            self.check(base_routes(repo={"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/master"}))

    def test_migrate_warnt_bei_anderem_default_branch(self):
        repo = {"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/master"}
        result, out = self.check(base_routes(repo=repo), mode="migrate")
        self.assertEqual(result, 0)
        self.assertIn("Default-Branch ist refs/heads/master", out)

    def test_initialisiertes_repo_ist_in_ordnung(self):
        result, _ = self.check(base_routes(items=tree("/README.md", "/.gitignore", "/LICENSE")))
        self.assertEqual(result, 0)

    def test_bestehendes_projekt_nur_mit_migrate(self):
        for items in (tree("/README.md", "/project.yaml"), tree("/README.md", folders=("/src",))):
            with self.subTest(items=items), self.assertRaisesRegex(scaffold.ScaffoldError, "mode migrate"):
                self.check(base_routes(items=items, commits=[{"comment": "Initial commit"}]))

    def test_migrate_mit_inhalt(self):
        result, _ = self.check(base_routes(items=tree("/project.yaml"), commits=[{"comment": "Initial commit"}]), mode="migrate")
        self.assertEqual(result, 0)

    def test_erneuter_lauf_nach_dem_scaffold_commit(self):
        result, out = self.check(base_routes(items=tree("/README.md", "/project.yaml"),
                                             commits=[{"comment": "seed-scaffold: seed-template v0.19.0 eingeführt", "commitId": "a" * 40}]))
        self.assertEqual(result, 0)
        self.assertIn("schon eingeführt", out)

    def test_andere_service_connection_mit_gleichem_namen(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "andere Service Connection github-example"):
            self.check(base_routes(target_endpoints=({"id": "fremd", "name": "github-example"},)))

    def test_schon_geteilte_service_connection(self):
        result, _ = self.check(base_routes(target_endpoints=({"id": "e-github", "name": "github-example"},)))
        self.assertEqual(result, 0)

    def test_neues_projekt_noch_nicht_vorhanden(self):
        routes = base_routes()
        routes[("GET", f"{ORG}/_apis/projects/Projekt-0099")] = None
        result, out = self.check(routes, mode="createProject", ado_project="Projekt-0099", repository="")
        self.assertEqual(result, 0)
        self.assertIn("legt es an", out)

    def test_neues_projekt_ausserhalb_des_musters(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht vorgesehen"):
            self.check(base_routes(), mode="createProject", ado_project="Fremd")

    def test_neues_projekt_gibt_es_schon_ohne_mitgliedschaft(self):
        # Sonst nähme das Scaffold die Person in ein fremdes Projekt auf.
        with self.assertRaisesRegex(scaffold.ScaffoldError, "gibt es schon"):
            self.check(base_routes(member_of=(OTHER_GROUP,)), mode="createProject")

    def test_neues_projekt_gibt_es_schon_mit_mitgliedschaft(self):
        result, out = self.check(base_routes(), mode="createProject")
        self.assertEqual(result, 0)
        self.assertIn("gibt es schon", out)


class ShareTest(unittest.TestCase):
    def test_teilt_nur_was_fehlt_unter_demselben_namen(self):
        shared = {"sc-seed"}
        target = [{"id": "e-azure", "name": "sc-seed"}]
        routes = base_routes()

        def target_endpoints(url, body):
            return {"value": [e for e in target if f"endpointNames={e['name']}" in url]}

        def share(url, body):
            target.append({"id": url.split("/endpoints/")[1].split("?")[0], "name": body[0]["name"]})
            shared.add(body[0]["name"])
            return {}

        routes[("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints")] = target_endpoints
        routes[("PATCH", f"{ORG}/_apis/serviceendpoint/endpoints/*")] = share
        fake = FakeAzureDevOps(routes)
        result, out = quiet(scaffold.run_share, fake, CENTRAL, TARGET, ["sc-seed", "github-example"])
        self.assertEqual(result, 0)
        patches = [(url, body) for method, url, body in fake.calls if method == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertIn("/endpoints/e-github?", patches[0][0])
        self.assertEqual(patches[0][1], [{"name": "github-example", "description": "Sentinel Seed: geteilt vom zentralen Scaffold",
                                          "projectReference": {"id": TARGET, "name": "Projekt-0042"}}])
        self.assertIn("sc-seed: schon geteilt", out)

    def test_eigenes_projekt(self):
        fake = FakeAzureDevOps({})
        result, _ = quiet(scaffold.run_share, fake, CENTRAL, CENTRAL, ["sc-seed"])
        self.assertEqual((result, fake.calls), (0, []))


class FakeAz:
    """az-Aufrufe gegen einen Container im Speicher und eine Liste von Resource Groups."""

    def __init__(self, blobs=None, groups=(), race=None):
        self.blobs = dict(blobs or {})
        self.groups = list(groups)
        self.race = race
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        value = lambda flag: args[args.index(flag) + 1]
        if args[:2] == ["group", "list"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(self.groups), "")
        action = args[2]
        if action == "exists":
            return subprocess.CompletedProcess(args, 0, "true" if value("--name") in self.blobs else "false", "")
        if action == "list":
            prefix = value("--prefix")
            return subprocess.CompletedProcess(args, 0, "\n".join(b for b in self.blobs if b.startswith(prefix)), "")
        if action == "download":
            Path(value("--file")).write_text(self.blobs[value("--name")], encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        if action == "upload":
            name = value("--name")
            if self.race:
                self.blobs[name] = self.race
            if name in self.blobs:
                return subprocess.CompletedProcess(args, 1, "", "ErrorCode:BlobAlreadyExists")
            self.blobs[name] = Path(value("--file")).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def reserve_options(**overrides):
    values = dict(name="kundenportal", mode="new", account="stseed", container="tfstate", project_id=TARGET,
                  project_name="Projekt-0042", repository="kundenportal", environments=["dev", "prod"],
                  requested_by="Erika", build_id="17", dry_run=False)
    values.update(overrides)
    return scaffold.ReserveOptions(**values)


REGISTRY = "scaffold/registry/kundenportal.json"


class ReserveTest(unittest.TestCase):
    def reserve(self, az, **overrides):
        return quiet(scaffold.run_reserve, reserve_options(**overrides), az)

    def test_traegt_freien_namen_ein(self):
        az = FakeAz()
        result, _ = self.reserve(az)
        self.assertEqual(result, 0)
        entry = json.loads(az.blobs[REGISTRY])
        self.assertEqual((entry["projectId"], entry["adoProject"], entry["requestedBy"]), (TARGET, "Projekt-0042", "Erika"))
        upload = next(c for c in az.calls if "upload" in c)
        self.assertEqual(upload[upload.index("--if-none-match") + 1], "*")

    def test_erneuter_lauf_desselben_projekts(self):
        az = FakeAz({REGISTRY: json.dumps({"projectId": TARGET, "adoProject": "Projekt-0042"})})
        result, out = self.reserve(az)
        self.assertEqual(result, 0)
        self.assertIn("schon für Projekt-0042 eingetragen", out)

    def test_name_gehoert_anderem_projekt(self):
        az = FakeAz({REGISTRY: json.dumps({"projectId": "anders", "adoProject": "Projekt-0007"})})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Projekt-0007"):
            self.reserve(az)

    def test_vergeben_durch_state_scaffold_oder_resource_group(self):
        az = FakeAz({"kundenportal/dev.tfstate": "{}", "scaffold/kundenportal.tfstate": "{}"}, groups=["rg-kundenportal-dev"])
        with self.assertRaises(scaffold.ScaffoldError) as error:
            self.reserve(az)
        for part in ("kundenportal/", "scaffold/kundenportal.tfstate", "rg-kundenportal-dev"):
            self.assertIn(part, str(error.exception))
        self.assertNotIn(REGISTRY, az.blobs)

    def test_aehnlicher_name_stoert_nicht(self):
        az = FakeAz({"kundenportal-api/dev.tfstate": "{}"})
        result, _ = self.reserve(az)
        self.assertEqual(result, 0)
        query = next(c for c in az.calls if c[:2] == ["group", "list"])[3]
        self.assertIn("tags.\"seed:project\"=='kundenportal'", query)
        self.assertIn("'rg-kundenportal-prod'", query)

    def test_dry_run_traegt_nichts_ein(self):
        az = FakeAz()
        result, out = self.reserve(az, dry_run=True)
        self.assertEqual((result, az.blobs), (0, {}))
        self.assertIn("frei", out)

    def test_gleichzeitiger_lauf_anderes_projekt(self):
        az = FakeAz(race=json.dumps({"projectId": "anders", "adoProject": "Projekt-0007"}))
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Projekt-0007"):
            self.reserve(az)

    def test_enable_branch_policy_ohne_eintrag(self):
        az = FakeAz({"kundenportal/dev.tfstate": "{}"})
        result, out = self.reserve(az, mode="enableBranchPolicy")
        self.assertEqual((result, az.blobs.get(REGISTRY)), (0, None))
        self.assertIn("steht nicht in der Registry", out)


class RunTest(unittest.TestCase):
    def test_startet_lauf(self):
        fake = FakeAzureDevOps({("POST", f"{ORG}/{TARGET}/_apis/pipelines/12/runs"):
                                {"_links": {"web": {"href": "https://dev.azure.com/org/Projekt-0042/_build/results?buildId=5"}}}})
        result, out = quiet(scaffold.run_first_run, fake, TARGET, "12")
        self.assertEqual(result, 0)
        self.assertIn("buildId=5", out)

    def test_ungueltige_pipeline_id(self):
        with self.assertRaises(scaffold.ScaffoldError):
            quiet(scaffold.run_first_run, FakeAzureDevOps({}), TARGET, "")

    def test_berechtigt_service_connections_vor_dem_lauf(self):
        target = ({"id": "e-sc", "name": "sc-kundenportal"}, {"id": "e-github", "name": "github-example"})
        fake = FakeAzureDevOps({
            ("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints"): endpoints(*target),
            ("PATCH", f"{ORG}/{TARGET}/_apis/pipelines/pipelinePermissions/endpoint/*"): {},
        })
        result, out = quiet(scaffold.run_first_run, fake, TARGET, "12", ["sc-kundenportal", "github-example"], False)
        self.assertEqual(result, 0)
        patches = [(url, body) for method, url, body in fake.calls if method == "PATCH"]
        self.assertEqual([u.split("/endpoint/")[1].split("?")[0] for u, _ in patches], ["e-sc", "e-github"])
        self.assertEqual(patches[0][1], {"pipelines": [{"id": 12, "authorized": True}]})
        self.assertFalse(any(method == "POST" for method, _, _ in fake.calls))

    def test_fehlende_service_connection(self):
        fake = FakeAzureDevOps({("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints"): endpoints()})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "sc-kundenportal fehlt"):
            quiet(scaffold.run_first_run, fake, TARGET, "12", ["sc-kundenportal"])


# --- create-project ----------------------------------------------------------------------------

NEW = "n0000000-0000-0000-0000-000000000000"


def create_routes(exists=False, statuses=("inProgress", "succeeded"), member=False):
    state = {"created": exists, "statuses": list(statuses)}
    project = {"id": NEW, "name": "Projekt-0099"}

    def get_project(url, body):
        return project if state["created"] else None

    def post_project(url, body):
        state["created"] = True
        return {"id": "op1", "status": "queued"}

    def operation(url, body):
        return {"status": state["statuses"].pop(0) if len(state["statuses"]) > 1 else state["statuses"][0],
                "resultMessage": "kaputt"}

    def descriptors(url, body):
        return {"value": "scp.projekt" if f"/descriptors/{NEW}" in url else "aad.person"}

    return {
        ("GET", f"{ORG}/_apis/projects/Projekt-0099"): get_project,
        ("POST", f"{ORG}/_apis/projects"): post_project,
        ("GET", f"{ORG}/_apis/process/processes"): {"value": [{"id": "agile-id", "name": "Agile", "isDefault": True},
                                                               {"id": "basic-id", "name": "Basic"}]},
        ("GET", f"{ORG}/_apis/operations/op1"): operation,
        ("GET", f"{VSSPS}/_apis/graph/descriptors/*"): descriptors,
        ("GET", f"{VSSPS}/_apis/graph/groups"): {"value": [
            {"principalName": "[Projekt-0099]\\Project Administrators", "descriptor": "vssgp.admins"},
            {"principalName": "[Projekt-0099]\\Contributors", "descriptor": "vssgp.contributors"}]},
        ("GET", f"{VSSPS}/_apis/graph/memberships/*"): {"containerDescriptor": "vssgp.contributors"} if member else None,
        ("PUT", f"{VSSPS}/_apis/graph/memberships/*"): {},
    }


def create_options(**overrides):
    values = dict(project="Projekt-0099", name="kundenportal", requester_id=PERSON)
    values.update(overrides)
    return scaffold.CreateProjectOptions(**values)


class CreateProjectTest(unittest.TestCase):
    def create(self, fake, **overrides):
        return quiet(scaffold.run_create_project, fake, create_options(**overrides), lambda seconds: None)

    def test_legt_projekt_an_und_nimmt_person_auf(self):
        fake = FakeAzureDevOps(create_routes())
        result, out = self.create(fake)
        self.assertEqual(result, 0)
        post = next(body for method, url, body in fake.calls if method == "POST")
        self.assertEqual((post["name"], post["visibility"]), ("Projekt-0099", "private"))
        self.assertEqual(post["capabilities"]["processTemplate"]["templateTypeId"], "basic-id")
        self.assertEqual(post["capabilities"]["versioncontrol"]["sourceControlType"], "Git")
        put = next(url for method, url, body in fake.calls if method == "PUT")
        self.assertIn("/memberships/aad.person/vssgp.contributors?", put)
        # Weg J: Die Person klickt Initialize und startet den Lauf erneut.
        self.assertIn("Initialize", out)
        self.assertIn("mode new und adoProject Projekt-0099", out)
        self.assertIn("/Projekt-0099/_git/Projekt-0099", out)

    def test_vorhandenes_projekt_nur_pruefen(self):
        fake = FakeAzureDevOps(create_routes(exists=True))
        result, out = self.create(fake)
        self.assertEqual(result, 0)
        self.assertFalse([c for c in fake.calls if c[0] in ("POST", "PUT")])
        self.assertIn("gibt es schon", out)

    def test_schon_mitglied(self):
        fake = FakeAzureDevOps(create_routes(member=True))
        result, out = self.create(fake)
        self.assertEqual(result, 0)
        self.assertFalse([c for c in fake.calls if c[0] == "PUT"])
        self.assertIn("schon in Contributors", out)

    def test_dry_run_legt_nichts_an(self):
        fake = FakeAzureDevOps(create_routes())
        result, out = self.create(fake, dry_run=True)
        self.assertEqual(result, 0)
        self.assertFalse([c for c in fake.calls if c[0] != "GET"])

    def test_operation_scheitert(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "failed"):
            self.create(FakeAzureDevOps(create_routes(statuses=("failed",))))

    def test_operation_dauert_zu_lange(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht fertig"):
            self.create(FakeAzureDevOps(create_routes(statuses=("inProgress",))), timeout=6)

    def test_service_principal_ohne_klick(self):
        result, out = self.create(FakeAzureDevOps(create_routes()), auth="servicePrincipal")
        self.assertEqual(result, 0)
        self.assertNotIn("Initialize", out)

    def test_pat_modus(self):
        fake = FakeAzureDevOps(create_routes())
        with self.assertRaisesRegex(scaffold.ScaffoldError, "ab v0.20.0"):
            self.create(fake, auth="pat")
        self.assertEqual(fake.calls, [])


# --- fill (echtes git gegen lokale Repos) ------------------------------------------------------

GIT = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "init.defaultBranch=main",
       "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", "-c", "protocol.file.allow=always"]


def git(*args, cwd):
    return subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


class LocalRepo:
    """Ein Bare-Repo als Zielrepo; die Fake-API liest Refs, Baum und Commits daraus."""

    def __init__(self, path: Path, files: dict | None):
        self.path = str(path)
        git("init", "-q", "--bare", self.path, cwd=path.parent)
        if files:
            work = path.parent / f"{path.name}-init"
            work.mkdir()
            git("init", "-q", cwd=work)
            for name, content in files.items():
                (work / name).parent.mkdir(parents=True, exist_ok=True)
                (work / name).write_text(content, encoding="utf-8")
            git("add", "--all", cwd=work)
            git("commit", "-q", "-m", "Initial commit", cwd=work)
            git("push", "-q", self.path, "HEAD:refs/heads/main", cwd=work)

    def head(self):
        result = subprocess.run([*GIT, "--git-dir", self.path, "rev-parse", "--verify", "-q", "refs/heads/main"],
                                capture_output=True, text=True)
        return result.stdout.strip() or None

    def routes(self, repo_id="r1", name="kundenportal"):
        def repo(url, body):
            return {"id": repo_id, "name": name, "remoteUrl": self.path,
                    **({"defaultBranch": "refs/heads/main"} if self.head() else {})}

        def refs(url, body):
            head = self.head()
            return {"value": [{"name": "refs/heads/main", "objectId": head}] if head else []}

        def items(url, body):
            names = git("--git-dir", self.path, "ls-tree", "main", cwd=None).splitlines()
            return {"value": [{"path": "/", "isFolder": True}] + [
                {"path": "/" + line.split("\t")[1], "isFolder": line.split()[1] == "tree"} for line in names]}

        def commits(url, body):
            log = git("--git-dir", self.path, "log", "--format=%H %s", "main", cwd=None).splitlines()
            return {"value": [{"commitId": line[:40], "comment": line[41:]} for line in log]}

        api = f"{ORG}/{TARGET}/_apis/git/repositories"
        return {
            ("GET", f"{api}/{name}"): repo,
            ("GET", f"{api}/{repo_id}/refs"): refs,
            ("GET", f"{api}/{repo_id}/items"): items,
            ("GET", f"{api}/{repo_id}/commits"): commits,
        }

    def files(self):
        return sorted(git("--git-dir", self.path, "ls-tree", "-r", "--name-only", "main", cwd=None).splitlines())

    def show(self, name):
        return git("--git-dir", self.path, "show", f"main:{name}", cwd=None)


class FillTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        template = self.work / "seed-template"
        template.mkdir()
        git("init", "-q", cwd=template)
        for name, content in {"README.md": "# seed-template\n", "project.yaml": "project: x\n",
                              "infra/main.tf": "# infra\n", ".gitignore": "bin/\n"}.items():
            (template / name).parent.mkdir(parents=True, exist_ok=True)
            (template / name).write_text(content, encoding="utf-8")
        git("add", "--all", cwd=template)
        git("commit", "-q", "-m", "Template", cwd=template)
        git("tag", "v0.19.0", cwd=template)
        (template / "nach-dem-tag.md").write_text("später\n", encoding="utf-8")
        git("add", "--all", cwd=template)
        git("commit", "-q", "-m", "Später", cwd=template)
        self.template = str(template)

    def fill(self, target, **overrides):
        values = dict(project_id=TARGET, repository="kundenportal", template_url=self.template, template_ref="v0.19.0")
        values.update(overrides)
        fake = FakeAzureDevOps(target.routes())
        result, out = quiet(scaffold.run_fill, fake, scaffold.FillOptions(**values))
        return result, out, fake

    def test_ein_commit_auf_dem_initialisierten_repo(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# kundenportal\n", "LICENSE": "MIT\n"})
        before = target.head()
        result, out, _ = self.fill(target)
        self.assertEqual(result, 0)
        self.assertEqual(target.files(), [".gitignore", "LICENSE", "README.md", "infra/main.tf", "project.yaml"])
        self.assertEqual(target.show("README.md"), "# seed-template")
        parents = git("--git-dir", target.path, "log", "-1", "--format=%P %s", "main", cwd=None)
        # Genau ein Commit auf dem alten Stand, kein Merge.
        self.assertTrue(parents.startswith(f"{before} seed-scaffold: seed-template v0.19.0 eingeführt"), parents)
        self.assertIn("eingeführt", out)

    def test_erneuter_lauf_aendert_nichts(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# kundenportal\n"})
        self.fill(target)
        head = target.head()
        result, out, _ = self.fill(target)
        self.assertEqual((result, target.head()), (0, head))
        self.assertIn("schon eingeführt", out)

    def test_repo_mit_inhalt_nur_mit_migrate(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# x\n", "src/app.py": "print(1)\n"})
        head = target.head()
        with self.assertRaisesRegex(scaffold.ScaffoldError, "mode migrate"):
            self.fill(target)
        self.assertEqual(target.head(), head)

    def test_nur_release_tags(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# x\n"})
        for ref in ("main", "v1", "refs/tags/v0.19.0"):
            with self.subTest(ref=ref), self.assertRaisesRegex(scaffold.ScaffoldError, "Release-Tag"):
                self.fill(target, template_ref=ref)

    def test_unbekannter_tag(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# x\n"})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "v9.9.9 nicht lesbar"):
            self.fill(target, template_ref="v9.9.9")

    def test_leeres_repo_nur_mit_service_principal(self):
        target = LocalRepo(self.work / "target.git", None)
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Initialize"):
            self.fill(target)
        result, _, _ = self.fill(target, create_repository=True)
        self.assertEqual(result, 0)
        self.assertEqual(target.files(), [".gitignore", "README.md", "infra/main.tf", "project.yaml"])

    def test_dry_run(self):
        target = LocalRepo(self.work / "target.git", {"README.md": "# x\n"})
        head = target.head()
        result, out, _ = self.fill(target, dry_run=True)
        self.assertEqual((result, target.head()), (0, head))
        self.assertIn("dryRun", out)

    def test_service_principal_legt_fehlendes_repo_an(self):
        target = LocalRepo(self.work / "target.git", None)
        routes = target.routes()
        api = f"{ORG}/{TARGET}/_apis/git/repositories"
        created = []
        routes[("GET", f"{api}/kundenportal")] = lambda url, body: routes_repo(url, body) if created else None
        routes_repo = target.routes()[("GET", f"{api}/kundenportal")]
        routes[("POST", api)] = lambda url, body: created.append(body) or routes_repo(url, body)
        fake = FakeAzureDevOps(routes)
        options = scaffold.FillOptions(TARGET, "kundenportal", self.template, "v0.19.0", create_repository=True)
        result, _ = quiet(scaffold.run_fill, fake, options)
        self.assertEqual((result, created), (0, [{"name": "kundenportal"}]))
        self.assertIn("project.yaml", target.files())


# --- verify ------------------------------------------------------------------------------------

INIT, SCAFFOLD, TF1, TF2 = "0" * 39 + "1", "a" * 40, "b" * 40, "c" * 40
ME = "scaffold-id"


def push(push_id, old, new, by=ME):
    return {"pushId": push_id, "pushedBy": {"id": by, "displayName": "Erika" if by != ME else "Scaffold"},
            "refUpdates": [{"name": "refs/heads/main", "oldObjectId": old, "newObjectId": new}]}


def verify_routes(head, pushes, commits):
    """commits: {sha: (parents, paths)}"""
    api = f"{ORG}/{TARGET}/_apis/git/repositories"

    def commit(url, body):
        parts = url.split("?")[0].split("/commits/")[1].split("/")
        parents, paths = commits[parts[0]]
        if len(parts) > 1:
            return {"changes": [{"item": {"path": p}} for p in paths] + [{"item": {"path": "/", "isFolder": True}}]}
        return {"commitId": parts[0], "parents": parents}

    return {
        ("GET", f"{api}/kundenportal"): {"id": "r1"},
        ("GET", f"{api}/r1/refs"): {"value": [{"name": "refs/heads/main", "objectId": head}]},
        ("GET", f"{ORG}/_apis/connectionData"): {"authenticatedUser": {"id": ME}},
        ("GET", f"{api}/r1/pushes"): {"value": list(pushes)},
        ("GET", f"{api}/r1/commits/*"): commit,
    }


class VerifyTest(unittest.TestCase):
    def verify(self, routes, commit=SCAFFOLD):
        return quiet(scaffold.run_verify, FakeAzureDevOps(routes), TARGET, "kundenportal", commit)

    def test_nur_scaffold_commit(self):
        result, out = self.verify(verify_routes(SCAFFOLD, [push(1, INIT, SCAFFOLD)], {}))
        self.assertEqual(result, 0)
        self.assertIn("0 Commit(s) von Terraform", out)

    def test_commits_von_terraform(self):
        routes = verify_routes(TF2, [push(3, TF1, TF2), push(2, SCAFFOLD, TF1), push(1, INIT, SCAFFOLD)],
                               {TF2: ([TF1], ["/azure-pipelines.yml"]), TF1: ([SCAFFOLD], ["/project.yaml"])})
        result, out = self.verify(routes)
        self.assertEqual(result, 0)
        self.assertIn("2 Commit(s) von Terraform", out)

    def test_fremder_push(self):
        routes = verify_routes(TF1, [push(2, SCAFFOLD, TF1, by="erika"), push(1, INIT, SCAFFOLD)],
                               {TF1: ([SCAFFOLD], ["/project.yaml"])})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht vom Scaffold"):
            self.verify(routes)

    def test_fremde_datei(self):
        routes = verify_routes(TF1, [push(2, SCAFFOLD, TF1), push(1, INIT, SCAFFOLD)],
                               {TF1: ([SCAFFOLD], ["/project.yaml", "/infra/main.tf"])})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "/infra/main.tf"):
            self.verify(routes)

    def test_merge_commit(self):
        routes = verify_routes(TF1, [push(2, SCAFFOLD, TF1), push(1, INIT, SCAFFOLD)],
                               {TF1: ([SCAFFOLD, INIT], ["/project.yaml"])})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "2 Eltern"):
            self.verify(routes)

    def test_main_ohne_nachvollziehbaren_weg(self):
        routes = verify_routes(TF1, [push(1, INIT, SCAFFOLD)], {TF1: ([SCAFFOLD], ["/project.yaml"])})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht nachvollziehbar"):
            self.verify(routes)

    def test_ungueltiger_commit(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "ungültig"):
            self.verify({}, commit="$(seedScaffoldCommit)")


# --- identity ----------------------------------------------------------------------------------

def completed(args, code=0, out="", err=""):
    return subprocess.CompletedProcess(args, code, out, err)


class FakeAzure:
    """az für die Projektidentität: Managed Identity, Resource Groups, Rollen, Graph, Federated Credentials."""

    def __init__(self, identity=None, groups=None, assignments=None, lock_role=True, app_roles=None,
                 credentials=None, principal_delay=0):
        self.identity = identity
        self.groups = dict(groups or {})
        self.assignments = list(assignments or [])
        self.lock_role = lock_role
        self.app_roles = list(app_roles or [])
        self.credentials = list(credentials or [])
        self.principal_delay = principal_delay
        self.calls = []

    def writes(self):
        return [c for c in self.calls if c[:2] not in (["account", "show"], ["identity", "show"], ["group", "show"])
                and c[:3] not in (["role", "definition", "list"], ["role", "assignment", "list"], ["ad", "sp", "show"],
                                  ["identity", "federated-credential", "list"])
                and not (c[0] == "rest" and "get" in c)]

    def __call__(self, args):
        self.calls.append(args)
        a = args[:-2] if args[-2:] in (["-o", "json"], ["-o", "none"]) else args
        value = lambda flag: a[a.index(flag) + 1]
        ok = lambda data=None: completed(args, out="" if data is None else json.dumps(data))
        missing = completed(args, 3, err="ERROR: (ResourceNotFound) The Resource was not found.")
        if a[:2] == ["account", "show"]:
            return ok({"id": "s1", "name": "Seed-Abo", "tenantId": "t1"})
        if a[:2] == ["identity", "show"]:
            return ok(self.identity) if self.identity else missing
        if a[:2] == ["identity", "create"]:
            self.identity = {"principalId": "pid", "clientId": "cid", "tags": {"seed:project": value("--name")[3:]}}
            return ok(self.identity)
        if a[:2] == ["group", "show"]:
            return ok(self.groups[value("--name")]) if value("--name") in self.groups else missing
        if a[:2] == ["group", "create"]:
            name = value("--name")
            tags = dict(t.split("=", 1) for t in a[a.index("--tags") + 1:])
            self.groups[name] = {"id": f"/subscriptions/s1/resourceGroups/{name}", "name": name, "tags": tags}
            return ok(self.groups[name])
        if a[:3] == ["role", "definition", "list"]:
            return ok([{"name": "lock-role"}] if self.lock_role else [])
        if a[:3] == ["role", "assignment", "list"]:
            return ok([x for x in self.assignments if x["scope"] == value("--scope") and x["role"] == value("--role")])
        if a[:3] == ["role", "assignment", "create"]:
            if self.principal_delay:
                self.principal_delay -= 1
                return completed(args, 1, err="PrincipalNotFound: Principal pid does not exist in the directory.")
            self.assignments.append({"principalId": value("--assignee-object-id"), "scope": value("--scope"),
                                     "role": value("--role"), "condition": value("--condition") if "--condition" in a else None})
            return ok()
        if a[:3] == ["role", "assignment", "update"]:
            updated = json.loads(Path(value("--role-assignment")[1:]).read_text(encoding="utf-8"))
            for x in self.assignments:
                if x["scope"] == updated["scope"] and x["role"] == updated["role"]:
                    x["condition"] = updated["condition"]
            return ok()
        if a[:3] == ["ad", "sp", "show"]:
            return ok({"id": "graph-sp", "appRoles": [{"id": "owned-by", "value": "Application.ReadWrite.OwnedBy"}]})
        if a[0] == "rest":
            if value("--method") == "get":
                return ok({"value": self.app_roles})
            self.app_roles.append(json.loads(value("--body")))
            return ok()
        if a[:3] == ["identity", "federated-credential", "list"]:
            return ok(self.credentials)
        if a[:3] in (["identity", "federated-credential", "create"], ["identity", "federated-credential", "update"]):
            self.credentials = [c for c in self.credentials if c["name"] != value("--name")]
            self.credentials.append({"name": value("--name"), "issuer": value("--issuer"), "subject": value("--subject")})
            return ok()
        raise AssertionError(args)


def identity_options(**overrides):
    values = dict(name="kundenportal", environments=["dev", "prod"], location="westeurope",
                  identity_resource_group="rg-seed-identities", state_resource_group="rg-seed-tfstate",
                  state_account="stseedtf0000", container="tfstate")
    values.update(overrides)
    return scaffold.IdentityOptions(**values)


CONTAINER_SCOPE = ("/subscriptions/s1/resourceGroups/rg-seed-tfstate/providers/Microsoft.Storage/storageAccounts/"
                   "stseedtf0000/blobServices/default/containers/tfstate")


class IdentityTest(unittest.TestCase):
    def identity(self, az, **overrides):
        return quiet(scaffold.run_identity, identity_options(**overrides), az, lambda seconds: None)

    def test_legt_identitaet_gruppen_und_rollen_an(self):
        az = FakeAzure()
        result, _ = self.identity(az)
        self.assertEqual(result, 0)
        create = next(c for c in az.calls if c[:2] == ["identity", "create"])
        self.assertEqual((create[create.index("--name") + 1], create[create.index("--resource-group") + 1]),
                         ("id-kundenportal", "rg-seed-identities"))
        self.assertEqual(sorted(az.groups), ["rg-kundenportal-dev", "rg-kundenportal-prod"])
        self.assertEqual(az.groups["rg-kundenportal-prod"]["tags"], {"seed:project": "kundenportal", "seed:environment": "prod"})
        roles = {(x["scope"].rsplit("/", 1)[-1], x["role"]) for x in az.assignments}
        for group in ("rg-kundenportal-dev", "rg-kundenportal-prod"):
            for role in (scaffold.ROLE_CONTRIBUTOR, scaffold.ROLE_RBAC_ADMIN, "lock-role"):
                self.assertIn((group, role), roles)
        # Nichts auf der Subscription, nur Resource Groups und der State-Container.
        self.assertTrue(all(x["scope"].startswith("/subscriptions/s1/resourceGroups/") for x in az.assignments))
        self.assertEqual(len(az.assignments), 7)
        rbac = next(x for x in az.assignments if x["role"] == scaffold.ROLE_RBAC_ADMIN)
        for role in scaffold.SEED_ASSIGNABLE_ROLES:
            self.assertIn(role, rbac["condition"])
        self.assertNotIn(scaffold.ROLE_CONTRIBUTOR, rbac["condition"])
        blob = next(x for x in az.assignments if x["scope"] == CONTAINER_SCOPE)
        self.assertEqual(blob["role"], scaffold.ROLE_BLOB_DATA_CONTRIBUTOR)
        self.assertIn("StringStartsWith 'kundenportal/'", blob["condition"])
        self.assertIn("StringStartsWith 'plans/kundenportal/'", blob["condition"])
        self.assertEqual(az.app_roles, [])

    def test_owner_als_tag(self):
        az = FakeAzure()
        self.identity(az, environments=["dev"], owner="team@example.org")
        self.assertEqual(az.groups["rg-kundenportal-dev"]["tags"],
                         {"seed:project": "kundenportal", "seed:environment": "dev", "owner": "team@example.org"})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "owner"):
            self.identity(FakeAzure(), owner="kein-mail")

    def test_graph_rolle_nur_mit_sso(self):
        az = FakeAzure()
        self.identity(az, sso=True)
        self.assertEqual(az.app_roles, [{"principalId": "pid", "resourceId": "graph-sp", "appRoleId": "owned-by"}])

    def test_erneuter_lauf_aendert_nichts(self):
        az = FakeAzure()
        self.identity(az, sso=True)
        az.calls.clear()
        result, out = self.identity(az, sso=True)
        self.assertEqual((result, az.writes()), (0, []))
        self.assertNotIn("[neu]", out)

    def test_aeltere_bedingung_wird_ersetzt(self):
        az = FakeAzure()
        self.identity(az)
        rbac = next(x for x in az.assignments if x["role"] == scaffold.ROLE_RBAC_ADMIN)
        rbac["condition"] = "alt"
        result, out = self.identity(az)
        self.assertEqual(result, 0)
        self.assertIn("Bedingung aktualisiert", out)
        self.assertNotEqual(rbac["condition"], "alt")

    def test_fremde_resource_group(self):
        az = FakeAzure(groups={"rg-kundenportal-dev": {"id": "x", "name": "rg-kundenportal-dev", "tags": {"seed:project": "anderes"}}})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "rg-kundenportal-dev gibt es schon"):
            self.identity(az)
        self.assertEqual(az.assignments, [])

    def test_fremde_identitaet(self):
        az = FakeAzure(identity={"principalId": "x", "clientId": "y", "tags": {}})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "id-kundenportal gibt es schon"):
            self.identity(az)

    def test_dry_run(self):
        az = FakeAzure()
        result, out = self.identity(az, dry_run=True)
        self.assertEqual((result, az.writes()), (0, []))
        self.assertIn("[fehlt] Managed Identity id-kundenportal", out)
        self.assertIn("[fehlt] Resource Group rg-kundenportal-prod", out)

    def test_neue_identitaet_noch_nicht_verteilt(self):
        az = FakeAzure(principal_delay=2)
        result, _ = self.identity(az)
        self.assertEqual((result, len(az.assignments)), (0, 7))

    def test_ohne_onboarding(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Initialize-SeedTenant"):
            self.identity(FakeAzure(lock_role=False))

    def test_reservierter_name(self):
        az = FakeAzure()
        with self.assertRaisesRegex(scaffold.ScaffoldError, "reserviert"):
            self.identity(az, name="plans")
        self.assertEqual(az.calls, [])


ISSUER = "https://login.microsoftonline.com/t1/v2.0"
SUBJECT = "/eid1/c/pub/t/abc/a/def/sc/org-id/sc-id"


class FederateTest(unittest.TestCase):
    def federate(self, az, **overrides):
        values = dict(name="kundenportal", identity_resource_group="rg-seed-identities", issuer=ISSUER, subject=SUBJECT)
        values.update(overrides)
        return quiet(scaffold.run_federate, scaffold.FederateOptions(**values), az)

    def test_legt_credential_an(self):
        az = FakeAzure()
        result, _ = self.federate(az)
        self.assertEqual(result, 0)
        self.assertEqual(az.credentials, [{"name": "ado-sc-kundenportal", "issuer": ISSUER, "subject": SUBJECT}])
        create = next(c for c in az.calls if c[:3] == ["identity", "federated-credential", "create"])
        self.assertEqual(create[create.index("--audiences") + 1], "api://AzureADTokenExchange")

    def test_vorhanden(self):
        az = FakeAzure(credentials=[{"name": "ado-sc-kundenportal", "issuer": ISSUER, "subject": SUBJECT}])
        self.federate(az)
        self.assertEqual(az.writes(), [])

    def test_neue_service_connection_aktualisiert(self):
        az = FakeAzure(credentials=[{"name": "ado-sc-kundenportal", "issuer": ISSUER, "subject": "/alt"}])
        self.federate(az)
        self.assertEqual([c[:3] for c in az.writes()], [["identity", "federated-credential", "update"]])

    def test_ungueltige_werte(self):
        for overrides in ({"issuer": "http://x"}, {"subject": ""}, {"subject": "a\n##vso[x]"}):
            with self.subTest(overrides=overrides), self.assertRaises(scaffold.ScaffoldError):
                self.federate(FakeAzure(), **overrides)

    def test_dry_run(self):
        az = FakeAzure()
        self.federate(az, dry_run=True)
        self.assertEqual(az.writes(), [])


# --- connection (migrate) ----------------------------------------------------------------------

def connection_routes(endpoints_state, checks):
    def get_endpoints(url, body):
        return {"value": [e for e in endpoints_state if f"endpointNames={e['name']}" in url]}

    def post_endpoint(url, body):
        endpoints_state.append({**body, "id": "e-new", "authorization": {
            "scheme": "WorkloadIdentityFederation",
            "parameters": {**body["authorization"]["parameters"], "workloadIdentityFederationIssuer": ISSUER,
                           "workloadIdentityFederationSubject": SUBJECT}}})
        return endpoints_state[-1]

    def post_check(url, body):
        checks.append({**body, "id": len(checks) + 1})
        return checks[-1]

    def patch_check(url, body):
        checks[:] = [body if c["id"] == body["id"] else c for c in checks]
        return body

    checks_url = f"{ORG}/{TARGET}/_apis/pipelines/checks/configurations"
    return {
        ("GET", f"{ORG}/_apis/projects/{TARGET}"): {"id": TARGET, "name": "Projekt-0042"},
        ("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints"): get_endpoints,
        ("POST", f"{ORG}/_apis/serviceendpoint/endpoints"): post_endpoint,
        ("GET", checks_url): lambda url, body: {"value": list(checks)},
        ("POST", checks_url): post_check,
        ("PATCH", f"{checks_url}/*"): patch_check,
    }


def connection_options(**overrides):
    values = dict(project_id=TARGET, name="kundenportal", tenant_id="t1", subscription_id="s1", subscription_name="Seed-Abo",
                  client_id="cid", allowed_branches=["refs/heads/main", "refs/tags/v*"], pipelines_version="v0.20.0")
    values.update(overrides)
    return scaffold.ConnectionOptions(**values)


class ConnectionTest(unittest.TestCase):
    def test_legt_service_connection_mit_pruefungen_an(self):
        state, checks = [], []
        fake = FakeAzureDevOps(connection_routes(state, checks))
        result, _ = quiet(scaffold.run_connection, fake, connection_options())
        self.assertEqual(result, 0)
        body = next(b for m, u, b in fake.calls if m == "POST" and "serviceendpoint" in u)
        self.assertEqual(body["name"], "sc-kundenportal")
        self.assertEqual(body["authorization"]["parameters"], {"tenantid": "t1", "serviceprincipalid": "cid"})
        self.assertEqual(body["serviceEndpointProjectReferences"][0]["projectReference"]["id"], TARGET)
        branch = next(c for c in checks if c["type"]["id"] == scaffold.BRANCH_CONTROL_CHECK)
        self.assertEqual(branch["settings"]["inputs"]["allowedBranches"], "refs/heads/main,refs/tags/v*")
        template = next(c for c in checks if c["type"]["id"] == scaffold.REQUIRED_TEMPLATE_CHECK)
        self.assertEqual(template["settings"]["extendsChecks"], [{
            "repositoryType": "github", "repositoryName": "blackforestsentinel/seed-pipelines",
            "repositoryRef": "refs/tags/v0.20.0", "templatePath": "templates/web-app.yml"}])

    def test_neuer_tag_kommt_dazu(self):
        state, checks = [], []
        quiet(scaffold.run_connection, FakeAzureDevOps(connection_routes(state, checks)), connection_options(pipelines_version="v0.19.0"))
        fake = FakeAzureDevOps(connection_routes(state, checks))
        quiet(scaffold.run_connection, fake, connection_options())
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])
        template = next(c for c in checks if c["type"]["id"] == scaffold.REQUIRED_TEMPLATE_CHECK)
        self.assertEqual([e["repositoryRef"] for e in template["settings"]["extendsChecks"]],
                         ["refs/tags/v0.19.0", "refs/tags/v0.20.0"])

    def test_fremde_service_connection_gleichen_namens(self):
        state = [{"id": "x", "name": "sc-kundenportal", "authorization": {"parameters": {"serviceprincipalid": "andere"}}}]
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht für id-kundenportal"):
            quiet(scaffold.run_connection, FakeAzureDevOps(connection_routes(state, [])), connection_options())

    def test_ohne_identitaet(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Schritt identity"):
            quiet(scaffold.run_connection, FakeAzureDevOps({}), connection_options(client_id=""))


# --- Aufruf ------------------------------------------------------------------------------------

class MainTest(unittest.TestCase):
    def boom(self):
        raise AssertionError("Azure DevOps darf nicht aufgerufen werden")

    def test_pat_modus_vor_jedem_aufruf(self):
        for argv in (["check", "--name", "kundenportal", "--mode", "new", "--ado-project", "P", "--auth", "pat"],
                     ["create-project", "--ado-project", "P", "--name", "kundenportal", "--auth", "pat"],
                     ["fill", "--project-id", TARGET, "--repository", "r", "--template-url", "https://github.com/x/y.git",
                      "--template-ref", "v0.19.0", "--auth", "pat"]):
            with self.subTest(argv=argv[0]), self.assertRaisesRegex(scaffold.ScaffoldError, "pat gibt es ab v0.20.0"):
                scaffold.main(argv, ado_factory=self.boom)

    def test_template_url_nur_https_ohne_anmeldedaten(self):
        for url in ("http://github.com/x/y.git", "https://user:geheim@github.com/x/y.git", "ext::sh -c id", "/tmp/repo"):
            with self.subTest(url=url), self.assertRaisesRegex(scaffold.ScaffoldError, "templateUrl"):
                scaffold.main(["fill", "--project-id", TARGET, "--repository", "r", "--template-url", url,
                               "--template-ref", "v0.19.0"], ado_factory=self.boom)

    def test_token_aus_seed_azdo_token(self):
        saved = {k: os.environ.get(k) for k in ("SYSTEM_COLLECTIONURI", "SEED_AZDO_TOKEN", "SYSTEM_ACCESSTOKEN")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update(SYSTEM_COLLECTIONURI=ORG + "/", SEED_AZDO_TOKEN="entra-token", SYSTEM_ACCESSTOKEN="job-token")
        self.assertEqual(scaffold.AzureDevOps.from_environment().token, "entra-token")
        del os.environ["SEED_AZDO_TOKEN"]
        self.assertEqual(scaffold.AzureDevOps.from_environment().token, "job-token")


class CleanTest(unittest.TestCase):
    def test_keine_zeilenumbrueche_in_meldungen(self):
        self.assertNotIn("\n", scaffold.clean("a\n##vso[task.setvariable variable=x]1"))

    def test_keine_logging_befehle_in_meldungen(self):
        # Der Agent erkennt ##vso[ auch mitten in einer Zeile, etwa in "Fehler: …" auf stderr.
        for value in ("a ##vso[task.setvariable variable=x]1", "###vso[task.prependpath]/tmp", "a\r##vso[x]"):
            with self.subTest(value=value):
                self.assertNotIn("##vso[", scaffold.clean(value))


if __name__ == "__main__":
    unittest.main()
