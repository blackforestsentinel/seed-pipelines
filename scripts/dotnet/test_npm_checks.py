"""Tests für checks.py npm: Standardbibliothek und PyYAML (python3-yaml, wie auf den Agents).

Die Ausgaben von npm audit unter fixtures/npm stammen von npm 11 (Node 24) gegen registry.npmjs.org,
gekürzt auf wenige Advisories je Paket; die Fehlerfälle sind echten Ausgaben nachgebaut.

Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Laden über den Pfad wie in test_checks.py; beide Testdateien teilen sich das Modul.
checks = sys.modules.get("seed_dotnet_checks")
if checks is None:
    _spec = importlib.util.spec_from_file_location("seed_dotnet_checks", Path(__file__).with_name("checks.py"))
    checks = importlib.util.module_from_spec(_spec)
    sys.modules["seed_dotnet_checks"] = checks
    _spec.loader.exec_module(checks)

os.environ.pop("TF_BUILD", None)

FIXTURES = Path(__file__).with_name("fixtures") / "npm"
DEFAULT_REGISTRY = "https://registry.npmjs.org/"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def lock(dependencies=None, dev=None, **installed):
    """package-lock.json (lockfileVersion 3); installed: Paket=Version unter node_modules."""
    packages = {"": {"name": "app", "dependencies": dependencies or {}, "devDependencies": dev or {}}}
    for name, version in installed.items():
        name = name.replace("__", "/").replace("_", "-")
        packages[f"node_modules/{name}"] = {"version": version,
                                            "resolved": f"https://registry.npmjs.org/{name}/-/x-{version}.tgz"}
    return {"name": "app", "lockfileVersion": 3, "requires": True, "packages": packages}


def report(**packages):
    """Bericht wie npm audit --json; packages: Name=(isDirect, [(GHSA, Schwere), ...])."""
    vulnerabilities = {}
    for name, (direct, advisories) in packages.items():
        vulnerabilities[name] = {
            "name": name, "severity": "high", "isDirect": direct, "nodes": [f"node_modules/{name}"],
            "via": [{"source": 1, "name": name, "dependency": name, "title": f"Lücke in {name}",
                     "url": f"https://github.com/advisories/{ident}", "severity": severity, "range": "<2.0.0"}
                    for ident, severity in advisories],
            "effects": [], "range": "<2.0.0", "fixAvailable": True,
        }
    return json.dumps({"auditReportVersion": 2, "vulnerabilities": vulnerabilities,
                       "metadata": {"dependencies": {"prod": 3, "dev": 0, "total": 3}}})


class FakeNpm:
    """Antwortet wie npm audit --json bzw. npm config get registry, je Ordner relativ zum Checkout."""

    def __init__(self, root, audits=None, registry=DEFAULT_REGISTRY):
        self.root = Path(root).resolve()
        self.audits = audits or {}
        self.registry = registry
        self.calls = []

    def __call__(self, args, timeout, cwd=None):
        relative = Path(cwd).resolve().relative_to(self.root).as_posix()
        self.calls.append((args[1:], relative))
        if args[1:4] == ["config", "get", "registry"]:
            return checks.Result(0, self.registry + "\n", "")
        output = self.audits.get(relative, fixture("audit-clean.json"))
        if isinstance(output, checks.Result):
            return output
        # npm audit endet mit 1, sobald es eine Lücke gibt; die Prüfung liest nur das JSON.
        return checks.Result(1, output, "npm warn audit ...\n")


class Work(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project_yaml = self.root / "project.yaml"
        self.project_yaml.write_text("project: my-app\n", encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2), encoding="utf-8")

    def check(self, fake=None, text=None, npm="npm", state_file=None):
        if text is not None:
            self.project_yaml.write_text(text, encoding="utf-8")
        fake = fake or FakeNpm(self.root)
        log = checks.Log()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            checks.check_npm(self.root, str(self.project_yaml), npm, fake, log, state_file)
        return log, out.getvalue(), fake


class AuditTests(Work):
    def setUp(self):
        super().setUp()
        self.write("frontend/package-lock.json", fixture("lock-vulnerable.json"))

    def vulnerable(self, text=None, **kwargs):
        return self.check(FakeNpm(self.root, {"frontend": fixture("audit-vulnerable.json")}), text, **kwargs)

    def test_clean_runtime_dependencies(self):
        self.write("frontend/package-lock.json", lock({"react": "19.3.0"}, react="19.3.0"))
        log, out, fake = self.check()
        self.assertEqual((log.errors, log.warnings), (0, 0), out)
        self.assertIn("npm audit in frontend: 14 Laufzeit-Pakete", out)
        self.assertIn("Keine bekannten Lücken in den Laufzeit-Abhängigkeiten", out)
        # Nur Laufzeit-Abhängigkeiten, nur die Lock-Datei, nie offline; im Ordner des Projekts.
        audit = next(call for call in fake.calls if call[0][0] == "audit")
        self.assertEqual(audit, (["audit", "--json", "--omit=dev", "--package-lock-only", "--no-offline"], "frontend"))

    def test_one_error_per_package(self):
        log, out, _ = self.vulnerable()
        # uuid (Vorabversion), axios, lodash, react-router, turbo-stream
        self.assertEqual((log.errors, log.warnings), (5, 0), out)
        self.assertIn("Fehler: frontend/package-lock.json: npm-Paket axios 0.21.1 in frontend: 1 Lücke High oder "
                      "Critical: GHSA-cph5-m8f7-6c5x (high); dazu 2 Lücken Low oder Moderate.", out)
        self.assertIn("npm-Paket turbo-stream 2.4.0 in frontend (transitiv): 1 Lücke High oder Critical", out)
        self.assertIn("build.ignoreAdvisories eintragen", out)
        # Einzelheiten je Advisory im Log, ohne Annotation
        self.assertIn("  GHSA-wf5p-g6vw-rhxx (moderate) axios: Axios Cross-Site Request Forgery Vulnerability "
                      "https://github.com/advisories/GHSA-wf5p-g6vw-rhxx", out)
        # devDependencies prüft npm audit mit --omit=dev nicht; minimist steht nur im Bericht ohne.
        self.assertNotIn("minimist", out)

    def test_exceptions_per_advisory(self):
        text = """\
project: my-app
build:
  prereleasePackages:
    - name: UUID
      reason: Nur die Beta kann X, die stabile Version folgt.
  ignoreAdvisories:
    - id: GHSA-rxv8-25v2-qmq8
      reason: Kein Single-Fetch, das Frontend läuft als SPA.
    - id: https://github.com/advisories/GHSA-cph5-m8f7-6c5x
      reason: Nur feste URLs, keine Eingaben von außen.
"""
        log, out, _ = self.vulnerable(text)
        # lodash bleibt Fehler; axios und react-router behalten nur Low und Moderate.
        self.assertEqual((log.errors, log.warnings), (1, 2), out)
        self.assertIn("Fehler: frontend/package-lock.json: npm-Paket lodash 4.17.15", out)
        self.assertIn("Warnung: frontend/package-lock.json: npm-Paket axios 0.21.1 in frontend: 2 Lücken Low oder "
                      "Moderate: GHSA-wf5p-g6vw-rhxx (moderate), GHSA-xhjh-pmcv-23jw (low)", out)
        # Eine Advisory, zwei Pakete: Die Ausnahme gilt für beide.
        self.assertIn("Lücke GHSA-rxv8-25v2-qmq8 (high) in npm-Paket turbo-stream 2.4.0 in frontend (transitiv)", out)
        self.assertIn("Hingenommen laut", out)
        self.assertIn("Vorabversion erlaubt: npm-Paket uuid 9.0.0-beta.0 (frontend)", out)
        self.assertNotIn("nennt", out)

    def test_critical_is_error_moderate_and_low_warnings(self):
        self.write("frontend/package-lock.json", lock({"a": "1.0.0", "b": "1.0.0"}, a="1.0.0", b="1.0.0", c="1.0.0"))
        output = report(a=(True, [("GHSA-aaaa-aaaa-aaaa", "critical")]),
                        b=(True, [("GHSA-bbbb-bbbb-bbbb", "moderate")]),
                        c=(False, [("GHSA-cccc-cccc-cccc", "low"), ("GHSA-dddd-dddd-dddd", "info")]))
        log, out, _ = self.check(FakeNpm(self.root, {"frontend": output}))
        self.assertEqual((log.errors, log.warnings), (1, 2), out)
        self.assertIn("npm-Paket a 1.0.0 in frontend: 1 Lücke High oder Critical: GHSA-aaaa-aaaa-aaaa (critical).", out)
        self.assertIn("npm-Paket c 1.0.0 in frontend (transitiv): 2 Lücken Low oder Moderate", out)

    def test_many_advisories_are_cut(self):
        self.write("frontend/package-lock.json", lock({"a": "1.0.0"}, a="1.0.0"))
        output = report(a=(True, [(f"GHSA-{i:04}-xxxx-xxxx", "high") for i in range(checks.MAX_ADVISORIES + 3)]))
        log, out, _ = self.check(FakeNpm(self.root, {"frontend": output}))
        self.assertEqual(log.errors, 1)
        self.assertIn(f"{checks.MAX_ADVISORIES + 3} Lücken High oder Critical", out)
        self.assertIn("und 3 weitere (siehe Log)", out)

    def test_same_package_in_two_projects(self):
        self.write("extension/package-lock.json", fixture("lock-vulnerable.json"))
        fake = FakeNpm(self.root, {"frontend": fixture("audit-vulnerable.json"),
                                   "extension": fixture("audit-vulnerable.json")})
        log, out, _ = self.check(fake)
        self.assertEqual(log.errors, 5, out)
        self.assertIn("npm-Paket axios 0.21.1 in extension und frontend:", out)
        # Ohne Datei: zwei Lock-Dateien, also keine Annotation an einer davon.
        self.assertIn("Fehler: npm-Paket axios", out)

    def test_exit_code_does_not_matter(self):
        # audit-level=critical in einer .npmrc: Exit-Code 0 trotz Lücken High.
        fake = FakeNpm(self.root, {"frontend": checks.Result(0, fixture("audit-vulnerable.json"), "")})
        log, _, _ = self.check(fake)
        self.assertEqual(log.errors, 5)


class FailureTests(Work):
    TEXT = """\
project: my-app
build:
  ignoreAdvisories:
    - id: GHSA-aaaa-bbbb-cccc
      reason: Wird hier nicht mehr gefunden, zum Test.
"""

    def setUp(self):
        super().setUp()
        self.write("frontend/package-lock.json", lock({"react": "19.3.0"}, react="19.3.0"))

    def failing(self, output):
        return self.check(FakeNpm(self.root, {"frontend": output}), self.TEXT)

    def test_network_error_is_warning(self):
        log, out, _ = self.failing(fixture("audit-error-network.json"))
        self.assertEqual((log.errors, log.warnings), (0, 1), out)
        self.assertIn("Prüfung auf bekannte Lücken in frontend nicht möglich (npm audit: request to "
                      "http://127.0.0.1:9/-/npm/v1/security/advisories/bulk failed, reason: connect ECONNREFUSED", out)
        # Ohne Ergebnis kein Urteil über unbenutzte Einträge.
        self.assertIn("bleibt offen", out)
        self.assertNotIn("nennt", out)

    def test_missing_lock_and_timeout_and_old_npm(self):
        for output, expected in ((fixture("audit-error-enolock.json"), "ENOLOCK, This command requires an existing lockfile"),
                                 (checks.Result(None, "", "", 120, "nach 120 s abgebrochen"), "nach 120 s abgebrochen"),
                                 ('{"actions": [], "advisories": {}, "metadata": {}}', "npm ab Version 7"),
                                 (checks.Result(1, "kein JSON", "npm error code E500"), "Code 1: kein JSON | npm error code E500")):
            with self.subTest(expected=expected):
                log, out, _ = self.failing(output)
                self.assertEqual((log.errors, log.warnings), (0, 1), out)
                self.assertIn(expected, out)

    def test_registry_answer_cannot_issue_logging_commands(self):
        os.environ["TF_BUILD"] = "True"
        try:
            log, out, _ = self.failing(fixture("audit-error-401.json"))
        finally:
            os.environ.pop("TF_BUILD", None)
        self.assertEqual(log.warnings, 1)
        warning = next(line for line in out.splitlines() if line.startswith("##vso[task.logissue type=warning"))
        self.assertIn("Unable to authenticate, need: Bearer ##vso_[task.setvariable", warning)
        self.assertIn("HTTP 401", warning)
        self.assertTrue(all(line.count("##vso[") <= 1 for line in out.splitlines()))
        # Den Body der Antwort gibt die Prüfung gar nicht aus.
        self.assertNotIn("prependpath", out)

    def test_credentials_in_urls_are_removed(self):
        output = json.dumps({"message": "request to https://build:geheim@registry.example.org/-/npm/v1/security/"
                                        "advisories/bulk failed, reason: socket hang up"})
        log, out, _ = self.failing(output)
        self.assertIn("https://***@registry.example.org/", out)
        self.assertNotIn("geheim", out)

    def test_without_npm(self):
        log, out, fake = self.check(FakeNpm(self.root), npm=None)
        self.assertEqual((log.errors, log.warnings), (0, 1), out)
        self.assertIn("npm-Pakete nicht auf Lücken geprüft: kein npm auf dem Agent (frontend/package-lock.json). "
                      "Node einrichten, etwa mit UseNode@1 in preBuildSteps", out)
        self.assertEqual(fake.calls, [])

    def test_without_npm_and_without_projects(self):
        (self.root / "frontend" / "package-lock.json").unlink()
        log, out, _ = self.check(FakeNpm(self.root), npm=None)
        self.assertEqual((log.errors, log.warnings), (0, 0), out)
        self.assertIn("Keine package-lock.json im Repo", out)

    def test_private_registry_is_named(self):
        fake = FakeNpm(self.root, registry="https://build:geheim@pkgs.example.org/org/_packaging/feed/npm/registry/")
        log, out, _ = self.check(fake)
        self.assertEqual((log.errors, log.warnings), (0, 0))
        self.assertIn("frontend: npm audit fragt https://pkgs.example.org/org/_packaging/feed/npm/registry/", out)
        self.assertNotIn("geheim", out)
        log, out, _ = self.check(FakeNpm(self.root))
        self.assertNotIn("npm audit fragt", out)


class ProjectTests(Work):
    def test_finds_every_lock_outside_node_modules(self):
        clean = lock({"react": "19.3.0"}, react="19.3.0")
        for name in ("frontend/package-lock.json", "extension/package-lock.json", "tools/widget/npm-shrinkwrap.json",
                     "tools/widget/package-lock.json", "package-lock.json",
                     "frontend/node_modules/x/package-lock.json", "infra/.terraform/modules/m/package-lock.json",
                     "api/Api/bin/Release/package-lock.json"):
            self.write(name, clean)
        log, out, fake = self.check()
        self.assertEqual((log.errors, log.warnings), (0, 0), out)
        audited = sorted(cwd for args, cwd in fake.calls if args[0] == "audit")
        self.assertEqual(audited, [".", "extension", "frontend", "tools/widget"])
        self.assertIn("npm-Projekte: extension, frontend, tools/widget und Wurzelordner.", out)

    def test_package_json_without_lock(self):
        self.write("widget/package.json", {"name": "widget", "dependencies": {"left-pad": "1.3.0"}})
        self.write("legacy/package.json", {"name": "legacy", "dependencies": {"left-pad": "1.3.0"}})
        self.write("legacy/yarn.lock", "# yarn lockfile v1\n")
        self.write("scripts/package.json", {"type": "module", "devDependencies": {"eslint": "9.0.0"}})
        self.write("frontend/node_modules/x/package.json", {"dependencies": {"y": "1.0.0"}})
        log, out, fake = self.check()
        self.assertEqual((log.errors, log.warnings), (0, 2), out)
        self.assertIn("Warnung: widget/package.json: widget/package.json hat Laufzeit-Abhängigkeiten, aber keine "
                      "package-lock.json.", out)
        self.assertIn("legacy/package.json hat Laufzeit-Abhängigkeiten, aber keine package-lock.json (yarn.lock; "
                      "die Prüfung liest nur package-lock.json)", out)
        self.assertNotIn("scripts", out)
        self.assertEqual(fake.calls, [])

    def test_workspaces(self):
        # Wie npm install --package-lock-only mit workspaces ["packages/*"]: ein Lock im Wurzelordner.
        self.write("package.json", {"name": "root", "workspaces": ["packages/*"], "devDependencies": {"minimist": "1.2.0"}})
        self.write("packages/a/package.json", {"name": "@demo/a", "dependencies": {"lodash": "4.17.15", "uuid": "9.0.0-beta.0"}})
        self.write("packages/b/package.json", {"name": "@demo/b", "dependencies": {"@demo/a": "*"}})
        self.write("package-lock.json", {
            "name": "root", "lockfileVersion": 3, "requires": True,
            "packages": {
                "": {"name": "root", "workspaces": ["packages/*"], "devDependencies": {"minimist": "1.2.0"}},
                "node_modules/@demo/a": {"resolved": "packages/a", "link": True},
                "node_modules/@demo/b": {"resolved": "packages/b", "link": True},
                "node_modules/lodash": {"version": "4.17.15", "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.15.tgz"},
                "node_modules/minimist": {"version": "1.2.0", "dev": True},
                "packages/a/node_modules/uuid": {"version": "9.0.0-beta.0",
                                                 "resolved": "https://registry.npmjs.org/uuid/-/uuid-9.0.0-beta.0.tgz"},
                "packages/a": {"version": "1.0.0", "dependencies": {"lodash": "4.17.15", "uuid": "9.0.0-beta.0"}},
                "packages/b": {"version": "1.0.0-dev.1", "dependencies": {"@demo/a": "*"}},
            },
        })
        log, out, fake = self.check(FakeNpm(self.root, {".": report(lodash=(True, [("GHSA-p6mc-m468-83gw", "high")]))}))
        # Ein Aufruf für den ganzen Workspace; die Mitglieder ohne eigene Lock-Datei sind abgedeckt.
        self.assertEqual([cwd for args, cwd in fake.calls if args[0] == "audit"], ["."])
        self.assertNotIn("keine package-lock.json", out)
        self.assertEqual((log.errors, log.warnings), (2, 0), out)
        self.assertIn("Vorabversion ohne Begründung: npm-Paket uuid 9.0.0-beta.0 (Wurzelordner)", out)
        self.assertIn("npm-Paket lodash 4.17.15 in Wurzelordner: 1 Lücke High", out)
        # @demo/b ist ein Workspace (link), seine Version keine Vorabversion eines Pakets aus der Registry.
        self.assertNotIn("@demo/", out)

    def test_lockfile_version_1(self):
        self.write("legacy/package.json", {"name": "legacy", "dependencies": {"lodash": "4.17.15", "beta": "2.0.0-rc.1"},
                                           "devDependencies": {"tool": "1.0.0-alpha"}})
        self.write("legacy/package-lock.json", {
            "name": "legacy", "lockfileVersion": 1, "requires": True,
            "dependencies": {
                "lodash": {"version": "4.17.15"},
                "beta": {"version": "2.0.0-rc.1", "dependencies": {"inner": {"version": "0.1.0"}}},
                "tool": {"version": "1.0.0-alpha", "dev": True},
            },
        })
        output = report(inner=(False, [("GHSA-1111-2222-3333", "high")]))
        output = output.replace('"node_modules/inner"', '"node_modules/beta/node_modules/inner"')
        log, out, _ = self.check(FakeNpm(self.root, {"legacy": output}))
        self.assertEqual(log.errors, 2, out)
        self.assertIn("Vorabversion ohne Begründung: npm-Paket beta 2.0.0-rc.1 (legacy)", out)
        self.assertIn("npm-Paket inner 0.1.0 in legacy (transitiv)", out)
        self.assertNotIn("tool", out)

    def test_unreadable_lock(self):
        self.write("frontend/package-lock.json", "{ kaputt")
        log, out, fake = self.check(text="project: x\nbuild:\n  ignoreAdvisories:\n    - id: GHSA-aaaa-bbbb-cccc\n"
                                          "      reason: Zum Test der offenen Prüfung.\n")
        self.assertEqual((log.errors, log.warnings), (0, 1), out)
        self.assertIn("frontend/package-lock.json nicht lesbar", out)
        self.assertIn("bleibt offen", out)
        self.assertEqual(fake.calls, [])


class PrereleaseTests(Work):
    def test_only_direct_runtime_dependencies_from_a_registry(self):
        data = lock({"stable": "1.0.0", "beta": "2.0.0-beta.1", "from-git": "github:example/x", "local": "file:../x"},
                    dev={"tool": "1.0.0-rc.1"}, stable="1.0.0", beta="2.0.0-beta.1", tool="1.0.0-rc.1", deep="3.0.0-alpha")
        data["packages"][""]["optionalDependencies"] = {"opt": "1.0.0-next.2"}
        data["packages"]["node_modules/opt"] = {"version": "1.0.0-next.2", "optional": True}
        data["packages"]["node_modules/from-git"] = {"version": "0.1.0-dev", "resolved": "git+ssh://git@example.org/x.git#abc"}
        data["packages"]["node_modules/local"] = {"version": "0.1.0-dev", "resolved": "file:../x"}
        self.write("frontend/package-lock.json", data)
        log, out, _ = self.check()
        self.assertEqual((log.errors, log.warnings), (2, 0), out)
        self.assertIn("Fehler: frontend/package-lock.json: Vorabversion ohne Begründung: npm-Paket beta 2.0.0-beta.1 "
                      "(frontend). Eine stabile Version in package.json eintragen", out)
        self.assertIn("npm-Paket opt 1.0.0-next.2", out)
        for name in ("stable", "tool", "deep", "from-git", "local"):
            self.assertNotIn(f"npm-Paket {name} ", out)

    def test_build_metadata_is_not_prerelease(self):
        self.write("frontend/package-lock.json", lock({"x": "1.0.0+build.5"}, x="1.0.0+build.5"))
        log, _, _ = self.check()
        self.assertEqual(log.errors, 0)


class UnusedEntryTests(Work):
    TEXT = """\
project: my-app
build:
  prereleasePackages:
    - name: Example.Preview
      reason: NuGet-Vorabversion, die nur X kann.
  ignoreAdvisories:
    - id: GHSA-5crp-9r3c-p9vr
      reason: Betrifft nur Eingaben, die die API nicht annimmt.
"""

    def setUp(self):
        super().setUp()
        self.write("frontend/package-lock.json", lock({"react": "19.3.0"}, react="19.3.0"))
        self.state = self.root / "state.json"

    def nuget(self, prerelease=(True, []), advisories=(True, [])):
        checks.save_state(str(self.state), "nuget", {"prerelease": checks.Usage(prerelease[0], set(prerelease[1])),
                                                     "advisories": checks.Usage(advisories[0], set(advisories[1]))})

    def test_alone_judges_only_npm(self):
        log, out, _ = self.check(text=self.TEXT)
        self.assertEqual(log.warnings, 2, out)
        self.assertIn("build.prereleasePackages nennt Example.Preview, aber keine direkte Referenz (npm)", out)
        self.assertIn("build.ignoreAdvisories nennt GHSA-5crp-9r3c-p9vr, die Prüfung (npm) findet", out)

    def test_entries_used_by_nuget(self):
        self.nuget(prerelease=(True, ["example.preview"]), advisories=(True, ["ghsa-5crp-9r3c-p9vr"]))
        log, out, _ = self.check(text=self.TEXT, state_file=str(self.state))
        self.assertEqual((log.errors, log.warnings), (0, 0), out)

    def test_entries_used_by_neither(self):
        self.nuget()
        log, out, _ = self.check(text=self.TEXT, state_file=str(self.state))
        self.assertEqual(log.warnings, 2, out)
        self.assertIn("Warnung: ", out)
        self.assertIn(":7: build.ignoreAdvisories nennt GHSA-5crp-9r3c-p9vr, die Prüfung (NuGet und npm) findet "
                      "diese Lücke nicht mehr.", out)

    def test_entry_used_by_npm_only(self):
        self.write("frontend/package-lock.json", lock({"x": "1.0.0"}, x="1.0.0"))
        self.nuget(prerelease=(True, ["example.preview"]))
        fake = FakeNpm(self.root, {"frontend": report(x=(True, [("GHSA-5crp-9r3c-p9vr", "high")]))})
        log, out, _ = self.check(fake, self.TEXT, state_file=str(self.state))
        self.assertEqual((log.errors, log.warnings), (0, 0), out)

    def test_open_when_nuget_did_not_run(self):
        self.nuget(prerelease=(False, []), advisories=(False, []))
        log, out, _ = self.check(text=self.TEXT, state_file=str(self.state))
        self.assertEqual(log.warnings, 0, out)
        self.assertIn("Ob alle Einträge in build.ignoreAdvisories noch nötig sind, bleibt offen", out)

    def test_open_without_nuget_result(self):
        log, out, _ = self.check(text=self.TEXT, state_file=str(self.state))
        self.assertEqual(log.warnings, 0, out)
        self.assertIn("Kein Ergebnis von „NuGet-Pakete prüfen“", out)

    def test_config_errors(self):
        log, out, fake = self.check(text="project: x\nbuild:\n  ignoreAdvisories:\n    - id: GHSA-aaaa-bbbb-cccc\n")
        self.assertEqual(log.errors, 1)
        self.assertIn("GHSA-aaaa-bbbb-cccc ohne reason", out)
        log, out, fake = self.check(text="project: x\nbuild:\n  - a\n b: c\n")
        self.assertEqual(log.errors, 1)
        self.assertEqual(fake.calls, [])


class HelperTests(unittest.TestCase):
    def test_lock_version(self):
        data = json.loads(fixture("lock-vulnerable.json"))
        self.assertEqual(checks.lock_version(data, "node_modules/axios"), "0.21.1")
        self.assertEqual(checks.lock_version(data, "node_modules/fehlt"), "")
        legacy = {"lockfileVersion": 1, "dependencies": {"@scope/a": {"version": "1.0.0", "dependencies": {"b": {"version": "2.0.0"}}}}}
        self.assertEqual(checks.lock_version(legacy, "node_modules/@scope/a/node_modules/b"), "2.0.0")
        self.assertEqual(checks.lock_version(legacy, "node_modules/b"), "")

    def test_parse_npm_json(self):
        self.assertEqual(checks.parse_npm_json("npm warn x\n{\n  \"a\": 1\n}\n"), {"a": 1})
        self.assertIsNone(checks.parse_npm_json("npm error code E401"))

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "project.yaml").write_text("project: x\n", encoding="utf-8")
            args = ["npm", "--root", directory, "--project-yaml", str(root / "project.yaml"), "--npm", "gibt-es-nicht-npm"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(args), 0)
            (root / "app").mkdir()
            (root / "app" / "package-lock.json").write_text(json.dumps(lock({"b": "1.0.0-beta"}, b="1.0.0-beta")), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(checks.main(args), 1)
            self.assertIn("kein npm auf dem Agent", out.getvalue())


if __name__ == "__main__":
    unittest.main()
