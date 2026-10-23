"""Tests für scaffold.py, nur Standardbibliothek.

Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
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


def base_routes(member_of=(CONTRIBUTORS,), repo=None, target_endpoints=(), yaml=None, commits=()):
    repo = repo if repo is not None else {"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/main"}
    central_endpoints = ({"id": "e-azure", "name": "sc-seed"}, {"id": "e-github", "name": "github-example"})
    return {
        ("GET", f"{ORG}/_apis/projects/P0042"): {"id": TARGET, "name": "P0042"},
        ("GET", f"{ORG}/_apis/projects/{TARGET}"): {"id": TARGET, "name": "P0042"},
        ("GET", f"{VSSPS}/_apis/identities"): lambda url, body: (
            {"value": [{"descriptor": "user", "memberOf": list(member_of), "providerDisplayName": "Erika"}]}
            if "identityIds=" in url else
            {"value": [{"isContainer": True, "providerDisplayName": "[P0042]\\Contributors", "descriptor": CONTRIBUTORS}]}),
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/kundenportal"): repo or None,
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/r1/items"): yaml,
        ("GET", f"{ORG}/{TARGET}/_apis/git/repositories/r1/commits"): {"value": list(commits)},
        ("GET", f"{ORG}/{CENTRAL}/_apis/serviceendpoint/endpoints"): endpoints(*central_endpoints),
        ("GET", f"{ORG}/{TARGET}/_apis/serviceendpoint/endpoints"): endpoints(*target_endpoints),
    }


def options(**overrides):
    values = dict(name="kundenportal", mode="new", ado_project="P0042", repository="kundenportal",
                  allowed_projects=r"P\d{4}", member_group="Contributors", requester_id=PERSON,
                  connections=["sc-seed", "github-example"])
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
        self.assertIn("Mitgliedschaft in [P0042]\\Contributors: ja", out)

    def test_eingaben_werden_vor_jedem_aufruf_geprueft(self):
        fake = FakeAzureDevOps({})
        with self.assertRaises(scaffold.ScaffoldError) as error:
            scaffold.run_check(fake, options(name='x"; echo INJIZIERT; "', repository="a b", mode="neu"), CENTRAL)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(str(error.exception).splitlines()), 3)

    def test_projektname_ohne_steuerzeichen(self):
        with self.assertRaises(scaffold.ScaffoldError):
            self.check(base_routes(), ado_project="P0042\n##vso[task.setvariable variable=x]1")

    def test_projekt_ausserhalb_des_musters(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "nicht vorgesehen"):
            self.check(base_routes(), allowed_projects=r"X\d{4}")

    def test_nicht_freigeschaltet(self):
        routes = base_routes()
        routes[("GET", f"{ORG}/_apis/projects/P0042")] = None
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Grant-SeedScaffoldAccess"):
            self.check(routes)

    def test_keine_mitgliedschaft(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "Mitglied von"):
            self.check(base_routes(member_of=(OTHER_GROUP,)))

    def test_repo_fehlt(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "New repository"):
            self.check(base_routes(repo={}))

    def test_leeres_repo(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "leer"):
            self.check(base_routes(repo={"id": "r1", "name": "kundenportal"}))

    def test_new_braucht_main(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "erwartet ist main"):
            self.check(base_routes(repo={"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/master"}))

    def test_migrate_warnt_bei_anderem_default_branch(self):
        repo = {"id": "r1", "name": "kundenportal", "defaultBranch": "refs/heads/master"}
        result, out = self.check(base_routes(repo=repo), mode="migrate")
        self.assertEqual(result, 0)
        self.assertIn("Default-Branch ist refs/heads/master", out)

    def test_bestehendes_projekt_nur_mit_migrate(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "mode migrate"):
            self.check(base_routes(yaml={"path": "/project.yaml"}, commits=[{"comment": "Initial commit"}]))

    def test_erneuter_lauf_nach_dem_merge(self):
        result, out = self.check(base_routes(yaml={"path": "/project.yaml"},
                                             commits=[{"comment": "seed-scaffold: seed-template eingeführt"}]))
        self.assertEqual(result, 0)
        self.assertIn("schon eingeführt", out)

    def test_andere_service_connection_mit_gleichem_namen(self):
        with self.assertRaisesRegex(scaffold.ScaffoldError, "andere Service Connection sc-seed"):
            self.check(base_routes(target_endpoints=({"id": "fremd", "name": "sc-seed"},)))

    def test_schon_geteilte_service_connection(self):
        result, _ = self.check(base_routes(target_endpoints=({"id": "e-azure", "name": "sc-seed"},)))
        self.assertEqual(result, 0)


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
                                          "projectReference": {"id": TARGET, "name": "P0042"}}])
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
                  project_name="P0042", repository="kundenportal", environments=["dev", "prod"],
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
        self.assertEqual((entry["projectId"], entry["adoProject"], entry["requestedBy"]), (TARGET, "P0042", "Erika"))
        upload = next(c for c in az.calls if "upload" in c)
        self.assertEqual(upload[upload.index("--if-none-match") + 1], "*")

    def test_erneuter_lauf_desselben_projekts(self):
        az = FakeAz({REGISTRY: json.dumps({"projectId": TARGET, "adoProject": "P0042"})})
        result, out = self.reserve(az)
        self.assertEqual(result, 0)
        self.assertIn("schon für P0042 eingetragen", out)

    def test_name_gehoert_anderem_projekt(self):
        az = FakeAz({REGISTRY: json.dumps({"projectId": "anders", "adoProject": "P0007"})})
        with self.assertRaisesRegex(scaffold.ScaffoldError, "P0007"):
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
        az = FakeAz(race=json.dumps({"projectId": "anders", "adoProject": "P0007"}))
        with self.assertRaisesRegex(scaffold.ScaffoldError, "P0007"):
            self.reserve(az)

    def test_enable_branch_policy_ohne_eintrag(self):
        az = FakeAz({"kundenportal/dev.tfstate": "{}"})
        result, out = self.reserve(az, mode="enableBranchPolicy")
        self.assertEqual((result, az.blobs.get(REGISTRY)), (0, None))
        self.assertIn("steht nicht in der Registry", out)


class RunTest(unittest.TestCase):
    def test_startet_lauf(self):
        fake = FakeAzureDevOps({("POST", f"{ORG}/{TARGET}/_apis/pipelines/12/runs"):
                                {"_links": {"web": {"href": "https://dev.azure.com/org/P0042/_build/results?buildId=5"}}}})
        result, out = quiet(scaffold.run_first_run, fake, TARGET, "12")
        self.assertEqual(result, 0)
        self.assertIn("buildId=5", out)

    def test_ungueltige_pipeline_id(self):
        with self.assertRaises(scaffold.ScaffoldError):
            quiet(scaffold.run_first_run, FakeAzureDevOps({}), TARGET, "")


class CleanTest(unittest.TestCase):
    def test_keine_zeilenumbrueche_in_meldungen(self):
        self.assertNotIn("\n", scaffold.clean("a\n##vso[task.setvariable variable=x]1"))


if __name__ == "__main__":
    unittest.main()
