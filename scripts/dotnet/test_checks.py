"""Tests für checks.py: Standardbibliothek und PyYAML (python3-yaml, wie auf den Agents).

Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Laden über den Pfad: Bei der Suche ab scripts/ heißt das Paket dieses Ordners schon „dotnet“.
_spec = importlib.util.spec_from_file_location("seed_dotnet_checks", Path(__file__).with_name("checks.py"))
checks = importlib.util.module_from_spec(_spec)
sys.modules["seed_dotnet_checks"] = checks
_spec.loader.exec_module(checks)

# Fehler sind hier gewollt; ohne TF_BUILD erscheinen sie nicht als Annotation im Lauf.
os.environ.pop("TF_BUILD", None)

ADVISORY = "https://github.com/advisories/GHSA-5crp-9r3c-p9vr"
MODERATE = "https://github.com/advisories/GHSA-wvxc-855f-jvrv"

PROJECT_YAML = """\
# Kopf
project: my-app
owner: team@example.org   # TODO(onboarding): Adresse des Teams

build:
  prereleasePackages:
    - name: Example.Preview
      reason: Nur die Vorabversion kann X; stabile Version folgt.
  ignoreAdvisories:
    - id: GHSA-5crp-9r3c-p9vr
      reason: Betrifft nur Eingaben, die die API nicht annimmt.
"""


def listing(*projects, parameters=""):
    """JSON wie dotnet list package --format json; projects: (Pfad, topLevel, transitive)."""
    return json.dumps({
        "version": 1,
        "parameters": parameters,
        "projects": [
            {"path": path, "frameworks": [{"framework": "net10.0", "topLevelPackages": top, "transitivePackages": transitive}]}
            for path, top, transitive in projects
        ],
    }, indent=2)


def package(name, version, requested=None, latest=None, vulnerabilities=(), auto=False):
    item = {"id": name, "requestedVersion": requested or version, "resolvedVersion": version}
    if latest:
        item["latestVersion"] = latest
    if vulnerabilities:
        item["vulnerabilities"] = [{"severity": s, "advisoryurl": u} for s, u in vulnerabilities]
    if auto:
        item["autoReferenced"] = True
    return item


class FakeDotnet:
    """Antwortet je nach Optionen wie dotnet list package bzw. dotnet format."""

    def __init__(self, plain="", vulnerable="", outdated="", fmt=None):
        self.outputs = {"plain": plain, "vulnerable": vulnerable, "outdated": outdated}
        self.fmt = fmt
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[1] == "format":
            code, report = self.fmt
            if report is not None:
                directory = args[args.index("--report") + 1]
                Path(directory, "format-report.json").write_text(json.dumps(report), encoding="utf-8")
            return checks.Result(code, "", "")
        kind = "vulnerable" if "--vulnerable" in args else "outdated" if "--outdated" in args else "plain"
        output = self.outputs[kind]
        if isinstance(output, checks.Result):
            return output
        return checks.Result(0, output, "")


class Work(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project_yaml = self.root / "project.yaml"
        self.project_yaml.write_text(PROJECT_YAML, encoding="utf-8")

    def tearDown(self):
        self.directory.cleanup()

    def packages(self, fake, text=None):
        if text is not None:
            self.project_yaml.write_text(text, encoding="utf-8")
        log = checks.Log()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            checks.check_packages("dotnet", "api/Api.slnx", str(self.project_yaml), fake, log)
        return log, out.getvalue()


class TodoTests(Work):
    def test_one_warning_per_marker_with_line(self):
        self.project_yaml.write_text(PROJECT_YAML + "monitoring:\n  recipients: []  # todo(onboarding) Verteiler\n"
                                     "  budget: {}  # TODO (onboarding): Betrag je Umgebung\n", encoding="utf-8")
        log = checks.Log()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            checks.check_todos(str(self.project_yaml), log)
        self.assertEqual((log.errors, log.warnings), (0, 3))
        self.assertIn(":3: Offenes TODO(onboarding)", out.getvalue())
        self.assertIn(":13: Offenes TODO(onboarding)", out.getvalue())
        self.assertIn(":14: Offenes TODO(onboarding)", out.getvalue())

    def test_annotation_has_file_and_line(self):
        log = checks.Log()
        os.environ["TF_BUILD"] = "True"
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                checks.check_todos(str(self.project_yaml), log)
        finally:
            os.environ.pop("TF_BUILD", None)
        first = out.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("##vso[task.logissue type=warning;sourcepath="))
        self.assertIn(";linenumber=3]", first)

    def test_none(self):
        self.project_yaml.write_text("project: my-app\n", encoding="utf-8")
        log = checks.Log()
        with contextlib.redirect_stdout(io.StringIO()):
            checks.check_todos(str(self.project_yaml), log)
        self.assertEqual((log.errors, log.warnings), (0, 0))


class ConfigTests(Work):
    def test_reads_entries_case_insensitive(self):
        config = checks.load_build_config(str(self.project_yaml))
        self.assertEqual(config.problems, [])
        self.assertEqual(config.prerelease["example.preview"].line, 7)
        self.assertEqual(config.advisories["ghsa-5crp-9r3c-p9vr"].key, "GHSA-5crp-9r3c-p9vr")

    def test_without_build(self):
        self.project_yaml.write_text("project: my-app\n", encoding="utf-8")
        config = checks.load_build_config(str(self.project_yaml))
        self.assertEqual((config.prerelease, config.advisories, config.problems), ({}, {}, []))

    def test_entries_need_reason_as_text(self):
        text = """\
project: my-app
build:
  prereleasePackages:
    - name: Ohne.Grund
    - name: Leerer.Grund
      reason: "  "
    - Nur.Name
    - reason: ohne Namen
  ignoreAdvisories:
    - id: GHSA-aaaa-bbbb-cccc
      reason: no
"""
        self.project_yaml.write_text(text, encoding="utf-8")
        config = checks.load_build_config(str(self.project_yaml))
        self.assertEqual(config.prerelease, {})
        self.assertEqual(config.advisories, {})
        self.assertEqual([line for _, line in config.problems], [4, 5, 3, 8, 10])
        messages = " ".join(message for message, _ in config.problems)
        self.assertIn("Ohne.Grund ohne reason", messages)
        self.assertIn("kein Objekt", messages)
        self.assertIn("Eintrag ohne name", messages)
        self.assertIn("kein Text", messages)

    def test_reason_needs_ten_characters(self):
        text = """\
project: my-app
build:
  prereleasePackages:
    - name: Zu.Kurz
      reason: Test
    - name: Mit.Rand
      reason: "   abc     "
    - name: Genau.Zehn
      reason: "0123456789"
"""
        self.project_yaml.write_text(text, encoding="utf-8")
        config = checks.load_build_config(str(self.project_yaml))
        self.assertEqual(list(config.prerelease), ["genau.zehn"])
        self.assertEqual([line for _, line in config.problems], [4, 6])
        self.assertIn("zu kurz", config.problems[0][0])

    def test_wrong_shapes(self):
        for text in ("build: [a]\n", "build:\n  prereleasePackages: Example\n"):
            with self.subTest(text=text):
                self.project_yaml.write_text("project: x\n" + text, encoding="utf-8")
                self.assertEqual(len(checks.load_build_config(str(self.project_yaml)).problems), 1)

    def test_invalid_yaml(self):
        self.project_yaml.write_text("project: x\nbuild:\n  - a\n b: c\n", encoding="utf-8")
        with self.assertRaises(checks.ConfigError) as error:
            checks.load_build_config(str(self.project_yaml))
        self.assertIn("kein gültiges YAML", str(error.exception))

    def test_unsafe_tags_are_rejected(self):
        # Kein Code aus YAML: Tags wie !!python/object lehnt der sichere Loader ab.
        self.project_yaml.write_text("project: !!python/object/apply:os.system ['true']\n", encoding="utf-8")
        with self.assertRaises(checks.ConfigError):
            checks.load_build_config(str(self.project_yaml))


class PrereleaseTests(Work):
    def run_plain(self, *top, text=None):
        fake = FakeDotnet(plain=listing(("/a/api/Api/Api.csproj", list(top), [])), vulnerable=listing(), outdated=listing())
        return self.packages(fake, text)

    def test_prerelease_without_reason_is_error(self):
        log, out = self.run_plain(package("Some.Lib", "11.0.0-preview.1.25", requested="11.0.0-preview.1.25"))
        self.assertEqual(log.errors, 1)
        self.assertIn("Vorabversion ohne Begründung: Some.Lib 11.0.0-preview.1.25 (Api)", out)
        # Der Eintrag Example.Preview trifft nichts mehr.
        self.assertIn("build.prereleasePackages nennt Example.Preview", out)

    def test_resolved_version_counts_not_text(self):
        # Zentral verwaltet (Directory.Packages.props) oder mit Platzhalter: entscheidend ist die aufgelöste Version.
        log, out = self.run_plain(package("Range.Lib", "2.0.0-beta.3", requested="[2.0.0-*, )"),
                                  package("Stable.Lib", "2.0.0+build.5", requested="2.0.0"))
        self.assertEqual(log.errors, 1)
        self.assertIn("Range.Lib 2.0.0-beta.3", out)
        self.assertNotIn("Stable.Lib", out)

    def test_exception_with_reason(self):
        log, out = self.run_plain(package("example.preview", "1.0.0-rc.1"))
        self.assertEqual((log.errors, log.warnings), (0, 1))   # nur der unbenutzte Eintrag in ignoreAdvisories
        self.assertIn("Vorabversion erlaubt: example.preview 1.0.0-rc.1 (Api). Grund laut", out)
        self.assertNotIn("prereleasePackages nennt", out)

    def test_transitive_and_implicit_packages_do_not_count(self):
        fake = FakeDotnet(plain=listing(("/a/api/Api/Api.csproj", [package("Sdk.Implicit", "10.0.0-rc.2", auto=True)],
                                         [package("Deep.Dependency", "3.0.0-alpha")])),
                          vulnerable=listing(), outdated=listing())
        log, out = self.packages(fake)
        self.assertEqual(log.errors, 0)
        self.assertNotIn("Deep.Dependency", out)


class VulnerabilityTests(Work):
    def run_vulnerable(self, top, transitive=(), text=None):
        fake = FakeDotnet(plain=listing(), outdated=listing(),
                          vulnerable=listing(("/a/api/Api/Api.csproj", list(top), list(transitive))))
        log, out = self.packages(fake, text)
        return log, out, fake

    def test_high_without_exception_is_error(self):
        log, out, fake = self.run_vulnerable([package("Old.Json", "12.0.1", vulnerabilities=[("High", MODERATE)])])
        self.assertEqual(log.errors, 1)
        self.assertIn("Lücke GHSA-wvxc-855f-jvrv (High) in Old.Json 12.0.1, Projekt Api", out)
        # Immer gegen nuget.org, auch wenn nuget.config nur einen privaten Feed nennt.
        vulnerable = next(call for call in fake.calls if "--vulnerable" in call)
        self.assertEqual(vulnerable[vulnerable.index("--source") + 1], checks.NUGET_ORG)
        self.assertIn("--include-transitive", vulnerable)

    def test_critical_transitive_is_error(self):
        log, out, _ = self.run_vulnerable([], [package("Deep.Lib", "1.0.0", vulnerabilities=[("Critical", MODERATE)])])
        self.assertEqual(log.errors, 1)
        self.assertIn("Projekt Api (transitiv)", out)

    def test_ignored_advisory(self):
        log, out, _ = self.run_vulnerable([package("Old.Json", "12.0.1", vulnerabilities=[("High", ADVISORY)])])
        self.assertEqual((log.errors, log.warnings), (0, 1))   # nur der unbenutzte Eintrag Example.Preview
        self.assertIn("Hingenommen laut", out)

    def test_ignored_by_url(self):
        text = PROJECT_YAML.replace("id: GHSA-5crp-9r3c-p9vr", f"id: {ADVISORY}")
        log, _, _ = self.run_vulnerable([package("Old.Json", "12.0.1", vulnerabilities=[("Critical", ADVISORY)])], text=text)
        self.assertEqual(log.errors, 0)

    def test_low_and_moderate_are_warnings(self):
        log, out, _ = self.run_vulnerable([package("Azure.Identity", "1.10.0", vulnerabilities=[("Moderate", MODERATE)]),
                                           package("Other", "1.0.0", vulnerabilities=[("Low", MODERATE + "x")])])
        self.assertEqual(log.errors, 0)
        self.assertIn("Warnung: Lücke GHSA-wvxc-855f-jvrv (Moderate)", out)
        self.assertIn("(Low)", out)

    def test_unused_advisory_entry_warns(self):
        log, out, _ = self.run_vulnerable([])
        self.assertIn("build.ignoreAdvisories nennt GHSA-5crp-9r3c-p9vr", out)
        self.assertEqual(log.errors, 0)

    def test_entry_without_reason_is_error(self):
        text = PROJECT_YAML.replace("      reason: Betrifft nur Eingaben, die die API nicht annimmt.\n", "")
        log, out, _ = self.run_vulnerable([package("Old.Json", "12.0.1", vulnerabilities=[("High", ADVISORY)])], text=text)
        self.assertEqual(log.errors, 2)   # der Eintrag selbst und die Lücke, die er nicht mehr deckt
        self.assertIn("GHSA-5crp-9r3c-p9vr ohne reason", out)


class OutdatedTests(Work):
    def test_one_warning_per_package(self):
        top = [package("Worker", "2.51.0", latest="2.52.0"), package("Missing.Internal", "1.0.0", latest="Not found at the sources")]
        fake = FakeDotnet(plain=listing(), vulnerable=listing(),
                          outdated=listing(("/a/api/Api/Api.csproj", top, []),
                                           ("/a/api/Api.Tests/Api.Tests.csproj", [package("Worker", "2.50.0", latest="2.52.0")], [])))
        log, out = self.packages(fake, "project: x\n")
        self.assertEqual((log.errors, log.warnings), (0, 1))
        self.assertIn("Veraltet: Worker 2.50.0 und 2.51.0, neueste Version 2.52.0 (Projekte Api und Api.Tests)", out)
        self.assertIn("Missing.Internal: keine Version in den Paketquellen gefunden", out)

    def test_seed_package_without_resolved_version_is_reported(self):
        # Ohne aufgelöste Version sieht sie auch „Seed-Versionen prüfen“ nicht; dann warnt diese Prüfung.
        top = [package("Bfs.Seed.Auth", "", requested="0.9.0", latest="0.10.0")]
        fake = FakeDotnet(plain=listing(), vulnerable=listing(), outdated=listing(("/a/api/Api/Api.csproj", top, [])))
        log, out = self.packages(fake, "project: x\n")
        self.assertEqual(log.warnings, 1)
        self.assertIn("Veraltet: Bfs.Seed.Auth 0.9.0, neueste Version 0.10.0", out)
        self.assertNotIn("übernimmt", out)

    def test_seed_packages_are_left_to_seed_versions(self):
        # Zurückliegende Bfs.Seed.*-Pakete meldet „Seed-Versionen prüfen“; hier kämen sie doppelt.
        top = [package("Bfs.Seed.Auth", "0.9.0", latest="0.10.0"), package("bfs.seed.openapi", "0.9.0", latest="0.10.0"),
               package("Bfs.SeedLike", "1.0.0", latest="2.0.0")]
        fake = FakeDotnet(plain=listing(), vulnerable=listing(), outdated=listing(("/a/api/Api/Api.csproj", top, [])))
        log, out = self.packages(fake, "project: x\n")
        self.assertEqual((log.errors, log.warnings), (0, 1))
        self.assertIn("Veraltet: Bfs.SeedLike 1.0.0", out)
        self.assertNotIn("Veraltet: Bfs.Seed.Auth", out)
        self.assertIn("das übernimmt „Seed-Versionen prüfen“: Bfs.Seed.Auth und bfs.seed.openapi", out)


class ToolFailureTests(Work):
    def test_network_and_tool_errors_are_warnings(self):
        failed = checks.Result(1, "error: Unable to load the service index for source https://example.org/v3/index.json.", "")
        timeout = checks.Result(None, "", "", 300, "nach 300 s abgebrochen")
        fake = FakeDotnet(plain=checks.Result(0, "Kein JSON", ""), vulnerable=failed, outdated=timeout)
        log, out = self.packages(fake)
        self.assertEqual((log.errors, log.warnings), (0, 3))
        self.assertIn("Prüfung auf bekannte Lücken nicht möglich (dotnet list package --vulnerable", out)
        self.assertIn("Unable to load the service index", out)
        self.assertIn("nach 300 s abgebrochen", out)
        # Ohne Ergebnis keine Hinweise auf unbenutzte Einträge.
        self.assertNotIn("nennt", out)

    def test_no_second_restore(self):
        fake = FakeDotnet(plain=listing(), vulnerable=listing(), outdated=listing())
        self.packages(fake, "project: x\n")
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(call[-1] == "--no-restore" for call in fake.calls))

    def test_older_sdk_without_no_restore(self):
        calls = []

        def runner(args, timeout):
            calls.append(args)
            if "--no-restore" in args:
                return checks.Result(1, "Unrecognized command or argument '--no-restore'.", "")
            return checks.Result(0, listing(), "")

        log, out = self.packages(runner, "project: x\n")
        self.assertEqual((log.errors, log.warnings), (0, 0))
        self.assertEqual(len(calls), 6)
        self.assertNotIn("nicht möglich", out)

    def test_json_after_sdk_notice(self):
        data = checks.parse_json("Willkommen bei .NET\n{\"x\": 1}\n" + listing(("/a/Api.csproj", [], [])))
        self.assertEqual(len(data["projects"]), 1)

    def test_run_reports_missing_command(self):
        result = checks.run(["/gibt/es/nicht/dotnet"], 5)
        self.assertIsNone(result.code)
        self.assertIn("ließ sich nicht starten", result.problem)

    def test_run_stops_after_timeout(self):
        result = checks.run([sys.executable, "-c", "import time; time.sleep(30)"], 1)
        self.assertIsNone(result.code)
        self.assertIn("nach 1 s abgebrochen", result.problem)


class FormatTests(Work):
    def run_format(self, code, report):
        fake = FakeDotnet(fmt=(code, report))
        log = checks.Log()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            checks.check_format("dotnet", "api/Api.slnx", fake, log, self.root)
        return log, out.getvalue(), fake

    def test_clean(self):
        log, out, fake = self.run_format(0, [])
        self.assertEqual((log.errors, log.warnings), (0, 0))
        self.assertEqual(fake.calls[0][1:5], ["format", "whitespace", "api/Api.slnx", "--verify-no-changes"])

    def test_changes_name_files_and_fix(self):
        report = [{
            "FilePath": str(self.root / "api" / "Api" / "Program.cs"),
            "FileChanges": [{"LineNumber": 4, "CharNumber": 2}, {"LineNumber": 1, "CharNumber": 7}],
        }]
        log, out, _ = self.run_format(2, report)
        self.assertEqual(log.errors, 2)
        self.assertIn("api/Api/Program.cs:1:7: Formatierung: api/Api/Program.cs weicht an 2 Stellen ab", out)
        self.assertIn("Lokal beheben mit „dotnet format whitespace api/Api.slnx“", out)

    def test_same_file_counted_once(self):
        # dotnet format nennt eine Datei je Formatierer (Leerraum, Zeilenende) gesondert.
        path = str(self.root / "api" / "Api" / "Program.cs")
        report = [
            {"FilePath": path, "FileChanges": [{"LineNumber": 3, "CharNumber": 1, "FormatDescription": "Leerraum"}]},
            {"FilePath": path, "FileChanges": [{"LineNumber": 1, "CharNumber": 9, "FormatDescription": "Zeilenende"},
                                               {"LineNumber": 3, "CharNumber": 1, "FormatDescription": "Leerraum"}]},
        ]
        log, out, _ = self.run_format(2, report)
        self.assertEqual(log.errors, 2)
        self.assertIn("api/Api/Program.cs weicht an 2 Stellen ab, zuerst Zeile 1", out)
        self.assertIn("1 Datei(en)", out)

    def test_many_files_are_cut(self):
        report = [{"FilePath": str(self.root / f"F{i:02}.cs"), "FileChanges": [{"LineNumber": 1, "CharNumber": 1}]}
                  for i in range(checks.MAX_FILES + 5)]
        log, out, _ = self.run_format(2, report)
        self.assertEqual(log.errors, checks.MAX_FILES + 1)
        self.assertIn(f"{checks.MAX_FILES + 5} Datei(en)", out)

    def test_tool_error_is_warning(self):
        log, out, _ = self.run_format(1, None)
        self.assertEqual((log.errors, log.warnings), (0, 1))
        self.assertIn("Formatierung nicht geprüft", out)


class SuppressionTests(unittest.TestCase):
    FILES = {
        ".editorconfig": """\
root = true

[*.cs]
# Testnamen wie Methode_Fall_Ergebnis
dotnet_diagnostic.CA1707.severity = none
dotnet_diagnostic.RS0030.severity = none
# Uhrzeit kommt hier aus einem eigenen Dienst, nicht aus DateTime.Now
dotnet_diagnostic.rs0030.severity=suggestion
dotnet_diagnostic.SEEDOPENAPI014.severity = warning
dotnet_analyzer_diagnostic.severity = none

[Migrations/**.cs]
# Migrationen erzeugt das Werkzeug
generated_code = true
""",
        "api/Api/Api.csproj": """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <NoWarn>$(NoWarn);CS1591;
      RS0030</NoWarn>
    <!-- OpenAPI-Hinweise prüft ein eigener Test -->
    <WarningsNotAsErrors>SEEDOPENAPI014</WarningsNotAsErrors>
    <WarningsAsErrors>RS0030</WarningsAsErrors>
    <!-- RS0030 hier nur erwähnt -->
    <Configuration>Release</Configuration>
    <MyRules>SEEDAUTH001</MyRules>
    <RunAnalyzers>false</RunAnalyzers>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Example.Analyzers" Version="1.0.0" ExcludeAssets="analyzers" />
  </ItemGroup>
</Project>
""",
        "api/Directory.Build.props": """\
<Project>
  <PropertyGroup>
    <!-- Verbotene APIs prüft hier ein eigener Test,
         weil der Analyzer generierten Code meldet -->
    <MSBuildWarningsAsMessages>RS0030</MSBuildWarningsAsMessages>
  </PropertyGroup>
</Project>
""",
        "Directory.Build.rsp": "-p:NoWarn=SEEDSUPPRESS001\n",
        "tools/analyzers.globalconfig": "is_global = true\n# kurz\ndotnet_diagnostic.SEEDOPENAPI015.severity = none\n",
        "api/Api/obj/Api.props": "<Project><PropertyGroup><NoWarn>RS0030</NoWarn></PropertyGroup></Project>\n",
        "api/Api/bin/Release/x.globalconfig": "dotnet_diagnostic.RS0030.severity = none\n",
        "frontend/node_modules/a/.editorconfig": "dotnet_diagnostic.RS0030.severity = none\n",
    }

    def scan(self, files):
        with tempfile.TemporaryDirectory() as directory:
            for name, text in files.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            log = checks.Log()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                checks.check_suppressions(Path(directory), log)
            return log, out.getvalue()

    def test_findings(self):
        log, out = self.scan(self.FILES)
        self.assertEqual((log.errors, log.warnings), (4, 4), out)
        # Fehler: geschützte Regel abgeschaltet, ohne ausreichende Begründung in der Zeile davor
        self.assertIn(".editorconfig, Zeile 6: RS0030, severity = none schaltet eine geschützte Regel ab, ohne", out)
        self.assertIn("api/Api/Api.csproj, Zeile 3: RS0030 in <NoWarn> schaltet", out)
        self.assertIn("Directory.Build.rsp, Zeile 1: SEEDSUPPRESS001", out)
        self.assertIn("tools/analyzers.globalconfig, Zeile 3: SEEDOPENAPI015, severity = none schaltet eine "
                      "geschützte Regel ab, die Begründung „kurz“ ist zu kurz", out)
        # Ausnahmen mit Grund im Log
        self.assertIn("Ausnahme: .editorconfig, Zeile 8: RS0030, severity = suggestion. Grund: Uhrzeit kommt", out)
        self.assertIn("Ausnahme: api/Api/Api.csproj, Zeile 6: SEEDOPENAPI014 in <WarningsNotAsErrors>", out)
        self.assertIn("Ausnahme: api/Directory.Build.props, Zeile 5: RS0030 in <MSBuildWarningsAsMessages>. Grund: "
                      "Verbotene APIs prüft hier ein eigener Test, weil der Analyzer generierten Code meldet", out)
        self.assertIn("Ausnahme: .editorconfig, Zeile 14: generated_code = true. Grund: Migrationen", out)
        # Warnungen: Analyzer pauschal aus, oder Regel an unklarer Stelle
        self.assertIn(".editorconfig, Zeile 10: dotnet_analyzer_diagnostic.severity = none schaltet Analyzer "
                      "pauschal ab", out)
        self.assertIn("api/Api/Api.csproj, Zeile 10: SEEDAUTH001 in <MyRules>", out)
        self.assertIn("api/Api/Api.csproj, Zeile 11: <RunAnalyzers>false</RunAnalyzers>", out)
        self.assertIn("api/Api/Api.csproj, Zeile 14: ExcludeAssets mit analyzers", out)
        # Nicht gemeldet: andere Regeln, WarningsAsErrors, Kommentare, bin, obj, node_modules
        self.assertNotIn("CA1707", out)
        self.assertNotIn("<WarningsAsErrors>", out)
        self.assertNotIn("Zeile 8: RS0030 in", out)
        for skipped in ("obj/", "bin/", "node_modules"):
            self.assertNotIn(skipped, out)

    def test_clean_repo(self):
        # Eigenschaften von Bfs.Seed.OpenApi heißen ähnlich wie die Codes, sind aber keine.
        csproj = ("<Project><PropertyGroup><NoWarn>CS1591</NoWarn>\n"
                  "<SeedOpenApiReadme>../../README.md</SeedOpenApiReadme></PropertyGroup></Project>\n")
        log, out = self.scan({".editorconfig": "[*.cs]\ndotnet_diagnostic.RS0030.severity = error\n",
                              "api/Api/Api.csproj": csproj})
        self.assertEqual((log.errors, log.warnings), (0, 0))
        self.assertIn("Keine Abschaltung von RS0030", out)

    def test_main_fails_on_finding(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".editorconfig").write_text("dotnet_diagnostic.SEEDAUTH001.severity = none\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(["suppressions", "--root", directory]), 1)


class LogTests(unittest.TestCase):
    def test_messages_cannot_issue_logging_commands(self):
        # Paketnamen, Pfade und Gründe kommen aus dem Repo; der Agent erkennt ##vso[ in jeder Zeile.
        evil = "x\n##vso[task.setvariable variable=x]1\r ##vso[task.prependpath]/tmp"
        log = checks.Log()
        os.environ["TF_BUILD"] = "True"
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                log.warning(evil, "a;b]c%.cs", 3, 4)
                log.error(evil)
                log.info(evil)
        finally:
            os.environ.pop("TF_BUILD", None)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("##vso[task.logissue type=warning;sourcepath=a%3Bb%5Dc%AZP25.cs;linenumber=3;columnnumber=4]"))
        self.assertEqual(lines[0].count("##vso["), 1)
        self.assertEqual(lines[1].count("##vso["), 1)
        self.assertNotIn("##vso[", lines[2])

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "project.yaml")
            path.write_text("project: x\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(["todos", "--project-yaml", str(path)]), 0)
                self.assertEqual(checks.main(["todos", "--project-yaml", str(path) + ".fehlt"]), 1)
            fake = FakeDotnet(fmt=(2, [{"FilePath": "x.cs", "FileChanges": [{"LineNumber": 1, "CharNumber": 1}]}]))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(["format", "--solution", "a.slnx"], runner=fake), 1)


if __name__ == "__main__":
    unittest.main()
