"""Tests für die Auswertung in templates/steps/terraform-scan.yml, nur Standardbibliothek.

Vom Repo-Root: python3 -m unittest discover -s tests/steps -p "test_*.py"
Trivy selbst läuft hier nicht; der Bericht ist nach dem JSON von trivy config (SchemaVersion 2)
nachgebaut.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import embedded

TEMPLATE = "templates/steps/terraform-scan.yml"
scan = embedded.load(TEMPLATE, "seed-terraform-scan")


def ruleset(name):
    """Regeln (id: reason) eines Parameters aus dem Template, ohne YAML-Bibliothek."""
    text = (embedded.ROOT / TEMPLATE).read_text(encoding="utf-8")
    section = text.split(f"  - name: {name}\n", 1)[1].split("\n  - name: ", 1)[0].split("\nsteps:", 1)[0]
    return dict(re.findall(r"- id: (\S+)\n\s+reason: (.+)", section))


SKIP = ruleset("skipChecks")
ERRORS = ruleset("errorChecks")


def misconfiguration(rule, severity, line, resource="azurerm_storage_account.this", status="FAIL"):
    return {
        "ID": rule, "Severity": severity, "Status": status, "Title": f"Titel {rule}",
        "Message": f"Meldung {rule}", "Resolution": "Einstellung setzen.",
        "PrimaryURL": f"https://avd.aquasec.com/misconfig/{rule.lower()}",
        "CauseMetadata": {"Resource": resource, "StartLine": line, "EndLine": line},
    }


def report(*results):
    return {"SchemaVersion": 2, "Results": list(results)}


class RulesetTests(unittest.TestCase):
    def test_every_rule_has_id_and_reason(self):
        self.assertGreater(len(SKIP), 5)
        for rules in (SKIP, ERRORS):
            for rule, reason in rules.items():
                self.assertRegex(rule, r"^AZU-\d{4}$")
                self.assertGreaterEqual(len(reason.strip()), 20, rule)

    def test_lists_do_not_overlap(self):
        self.assertFalse(set(SKIP) & set(ERRORS))

    def test_architecture_checks_are_off(self):
        # Seed ohne VNet: Netzwerkregeln der Seed-Module sind Absicht, kein Befund.
        self.assertIn("AZU-0012", SKIP)
        self.assertIn("AZU-0013", SKIP)

    def test_named_misconfigurations_stay_errors(self):
        # Beispiele der Anfrage: öffentlicher Storage, TLS, HTTPS, FTP, Purge-Schutz
        for rule in ("AZU-0007", "AZU-0011", "AZU-0008", "AZU-0004", "AZU-0071", "AZU-0016"):
            self.assertNotIn(rule, SKIP)
        for rule in ("AZU-0071", "AZU-0072", "AZU-0016"):
            self.assertIn(rule, ERRORS)


class IgnoreTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)

    def tearDown(self):
        self.work.cleanup()

    def ignores(self, text, name="main.tf"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return scan.find_ignores(self.root)

    def test_reason_in_same_line(self):
        found = self.ignores('# trivy:ignore:AZU-0011 Altes Lesegerät kann nur TLS 1.0\nresource "x" "y" {}\n')
        self.assertEqual(found, [("main.tf", 1, ["AZU-0011"], "Altes Lesegerät kann nur TLS 1.0", None)])

    def test_without_reason_is_a_problem(self):
        for line in ("#trivy:ignore:AZU-0011", "# trivy:ignore:AZU-0011 kurz", "# trivy:ignore:AZU-0011 -- ok.",
                     "#tfsec:ignore:azure-storage-use-secure-tls-policy"):
            with self.subTest(line=line):
                [(_, _, _, _, problem)] = self.ignores(line + "\n")
                self.assertEqual(problem, "ohne Begründung")

    def test_wildcard_and_empty_rule(self):
        for line in ("#trivy:ignore:* Alles aus, weil es eilt", "# trivy:ignore: ohne Regel, aber mit Text"):
            with self.subTest(line=line):
                [(_, _, _, _, problem)] = self.ignores(line + "\n")
                self.assertIn("keine einzelne Regel", problem)

    def test_expiry_several_rules_and_comment_styles(self):
        text = "\n".join([
            "# trivy:ignore:AZU-0011:exp:2027-12-31 Ablösung des Geräts bis Ende 2027",
            "#trivy:ignore:AZU-0011 trivy:ignore:AZU-0059 Altgerät braucht TLS 1.0",
            '  min_tls_version = "TLS1_0" // trivy:ignore:AZU-0011 Grund am Zeilenende',
            '  name = "a#b" # tfsec:ignore:AZU-0008 Grund hinter einem # im Wert',
        ])
        found = self.ignores(text + "\n")
        self.assertEqual([(f[2], f[4]) for f in found], [
            (["AZU-0011"], None), (["AZU-0011", "AZU-0059"], None), (["AZU-0011"], None), (["AZU-0008"], None)])
        self.assertEqual(found[0][3], "Ablösung des Geräts bis Ende 2027")

    def test_terraform_dir_and_strings_are_ignored(self):
        self.ignores("#trivy:ignore:AZU-0011\n", ".terraform/modules/core/main.tf")
        found = self.ignores('description = "trivy:ignore:AZU-0011"\n')
        self.assertEqual(found, [])


class EvaluateTests(unittest.TestCase):
    def evaluate(self, *results):
        return scan.evaluate(report(*results), SKIP, ERRORS)

    def test_severity_and_raised_rules(self):
        findings, counts = self.evaluate({
            "Target": "main.tf", "MisconfSummary": {"Successes": 7, "Failures": 4},
            "Misconfigurations": [
                misconfiguration("AZU-0011", "CRITICAL", 9),
                misconfiguration("AZU-0007", "HIGH", 20, "azurerm_storage_container.public"),
                misconfiguration("AZU-0056", "MEDIUM", 1),
                misconfiguration("AZU-0016", "MEDIUM", 30, "azurerm_key_vault.own"),
                misconfiguration("AZU-0002", "LOW", 40, status="PASS"),
            ]})
        self.assertEqual([(f[0], f[1], f[3]) for f in findings], [
            ("error", "AZU-0011", 9), ("error", "AZU-0007", 20), ("warning", "AZU-0056", 1), ("error", "AZU-0016", 30)])
        self.assertIn("Seed: ", findings[3][4])
        self.assertIn("Lösung: Einstellung setzen.", findings[0][4])
        self.assertEqual(counts["passed"], 7)

    def test_skip_list_and_old_ids(self):
        findings, counts = self.evaluate({
            "Target": "storage.tf",
            "Misconfigurations": [misconfiguration("AVD-AZU-0012", "CRITICAL", 3), misconfiguration("AZU-0058", "LOW", 5)]})
        self.assertEqual(findings, [])
        self.assertEqual(counts["skipped"], 2)

    def test_downloaded_modules_are_not_reported(self):
        findings, counts = self.evaluate(
            {"Target": "git::https:/github.com/org/seed-terraform.git/core?ref=v1.0.0/.terraform/modules/core/core/main.tf",
             "Misconfigurations": [misconfiguration("AZU-0011", "CRITICAL", 3)]},
            {"Target": "modules/own/main.tf", "Misconfigurations": [misconfiguration("AZU-0011", "CRITICAL", 3)]})
        self.assertEqual([f[2] for f in findings], ["modules/own/main.tf"])
        self.assertEqual(counts["modules"], 1)

    def test_duplicates_once(self):
        item = misconfiguration("AZU-0059", "HIGH", 8)
        findings, _ = self.evaluate({"Target": "main.tf", "Misconfigurations": [item, dict(item)]})
        self.assertEqual(len(findings), 1)


class MainTests(unittest.TestCase):
    """Das Skript wie in der Pipeline: eigener Prozess, Regelsatz aus der Umgebung."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name, "infra")
        self.root.mkdir()
        self.script = Path(self.work.name, "scan.py")
        self.script.write_text(embedded.source(TEMPLATE, "seed-terraform-scan"), encoding="utf-8")

    def tearDown(self):
        self.work.cleanup()

    def run_scan(self, data, files=None):
        for name, text in (files or {}).items():
            (self.root / name).write_text(text, encoding="utf-8")
        report_file = Path(self.work.name, "report.json")
        report_file.write_text(json.dumps(data), encoding="utf-8")
        env = dict(os.environ,
                   SEED_TRIVY_SKIP=json.dumps([{"id": k, "reason": v} for k, v in SKIP.items()]),
                   SEED_TRIVY_ERRORS=json.dumps([{"id": k, "reason": v} for k, v in ERRORS.items()]))
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(self.script), str(report_file), ".", "infra"],
                                cwd=self.root, env=env, capture_output=True, text=True, encoding="utf-8")
        return result.returncode, result.stdout

    def test_clean_project_passes(self):
        code, out = self.run_scan(report({"Target": ".", "MisconfSummary": {"Successes": 52, "Failures": 0}}),
                                  {"main.tf": 'module "core" {}\n'})
        self.assertEqual(code, 0)
        self.assertNotIn("##vso[task.logissue", out)
        self.assertIn("52 Prüfungen bestanden, 0 Fehler, 0 Warnungen", out)

    def test_error_with_location_and_hint(self):
        code, out = self.run_scan(report({"Target": "extra.tf", "Misconfigurations": [
            misconfiguration("AZU-0011", "CRITICAL", 7), misconfiguration("AZU-0056", "MEDIUM", 1)]}))
        self.assertEqual(code, 1)
        self.assertIn("##vso[task.logissue type=error;sourcepath=infra/extra.tf;linenumber=7]AZU-0011 (CRITICAL)", out)
        self.assertIn("##vso[task.logissue type=warning;sourcepath=infra/extra.tf;linenumber=1]AZU-0056 (MEDIUM)", out)
        self.assertIn("# trivy:ignore:<Regel> <Begründung>", out)

    def test_warnings_only_pass(self):
        code, _ = self.run_scan(report({"Target": "extra.tf", "Misconfigurations": [misconfiguration("AZU-0056", "MEDIUM", 1)]}))
        self.assertEqual(code, 0)

    def test_ignore_without_reason_fails_and_valid_ones_are_listed(self):
        code, out = self.run_scan(report(), {
            "a.tf": "#trivy:ignore:AZU-0011\n",
            "b.tf": "# trivy:ignore:AZU-0008 Gerät im Lager spricht nur HTTP\n",
        })
        self.assertEqual(code, 1)
        self.assertIn("##vso[task.logissue type=error;sourcepath=infra/a.tf;linenumber=1]Ausnahme AZU-0011 ohne Begründung", out)
        self.assertIn("infra/b.tf:1 AZU-0008: Gerät im Lager spricht nur HTTP", out)

    def test_project_ignore_files_only_warn(self):
        code, out = self.run_scan(report(), {".trivyignore": "AZU-0011\n"})
        self.assertEqual(code, 0)
        self.assertIn("##vso[task.logissue type=warning;sourcepath=infra/.trivyignore]", out)

    def test_logging_commands_from_repo_are_defused(self):
        code, out = self.run_scan(report(), {"a.tf": "# trivy:ignore:AZU-0011 ##vso[task.setvariable variable=x]1\n"})
        self.assertEqual(code, 0)
        self.assertNotIn("##vso[task.setvariable", out)
        self.assertIn("##vso_[task.setvariable", out)


if __name__ == "__main__":
    unittest.main()
