"""Einbindung der Prüfschritte in templates/web-app.yml, nur Standardbibliothek.

Ersetzt nicht die Vorschau (tests/preview), hält aber die Reihenfolge fest, die die Schritte
brauchen: Der Terraform-Scan wertet die Module aus terraform init aus, der Versionshinweis liest
die Variablen des Build-Jobs.
"""

import unittest

import embedded

WEB_APP = (embedded.ROOT / "templates/web-app.yml").read_text(encoding="utf-8")


def position(text):
    index = WEB_APP.find(text)
    if index < 0:
        raise AssertionError(f"web-app.yml enthält {text!r} nicht")
    return index


class WiringTests(unittest.TestCase):
    def test_scan_after_terraform_init(self):
        self.assertLess(position("displayName: terraform fmt und validate"), position("- template: steps/terraform-scan.yml"))
        self.assertLess(position("- template: steps/terraform-scan.yml"), position("displayName: dotnet restore"))

    def test_version_hint_early_in_build(self):
        self.assertLess(position("displayName: project.yaml abgleichen"), position("- template: steps/seed-versions.yml"))
        self.assertLess(position("- template: steps/seed-versions.yml"), position("- template: steps/terraform-install.yml"))

    def test_build_job_exposes_seed_ref(self):
        # Die Defaults in seed-versions.yml lesen genau diese Variablen.
        self.assertIn("seedPipelinesRef: $[ resources.repositories.seed.ref ]", WEB_APP)
        self.assertIn("seedPipelinesRepository: $[ resources.repositories.seed.name ]", WEB_APP)
        steps = (embedded.ROOT / "templates/steps/seed-versions.yml").read_text(encoding="utf-8")
        self.assertIn("default: $(seedPipelinesRef)", steps)
        self.assertIn("default: $(seedPipelinesRepository)", steps)

    def test_scans_not_only_in_pr_runs(self):
        # Beide Schritte stehen außerhalb der Bedingung für PR-Läufe, laufen also immer.
        start = position("- ${{ if or(eq(variables['Build.Reason'], 'PullRequest'), parameters.validationOnly) }}:")
        end = position("displayName: project.yaml abgleichen")
        self.assertNotIn("terraform-scan", WEB_APP[start:end])
        self.assertNotIn("seed-versions", WEB_APP[start:end])


if __name__ == "__main__":
    unittest.main()
