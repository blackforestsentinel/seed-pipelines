"""templates/scaffold.yml und seine Schritte, nur Standardbibliothek (PyYAML, wenn vorhanden).

Ersetzt keinen Lauf, hält aber fest, was die Sicherheit des Scaffolds trägt: die Reihenfolge aus
dem Plan (prüfen, Identität, Template, Terraform, verify, berechtigen), Eingaben nur über die
Umgebung, kein PAT-Modus mehr, und die Terraform-Werte aus der Umgebung (steps/scaffold-config.yml).
"""

import unittest

import embedded

SCAFFOLD = (embedded.ROOT / "templates/scaffold.yml").read_text(encoding="utf-8")
CONFIG = "templates/steps/scaffold-config.yml"

try:
    import yaml
except ImportError:  # pragma: no cover - auf den Agents und in der CI vorhanden
    yaml = None


def position(text, start=0):
    index = SCAFFOLD.find(text, start)
    if index < 0:
        raise AssertionError(f"scaffold.yml enthält {text!r} nicht")
    return index


class OrderTests(unittest.TestCase):
    def test_reihenfolge_bei_new(self):
        new = position("# --- new (und createProject mit servicePrincipal)")
        steps = ["displayName: Projektidentität anlegen", "displayName: seed-template ins Repo",
                 "displayName: terraform apply", "displayName: Federated Credential",
                 "displayName: main prüfen", "displayName: Pipeline berechtigen und ersten Lauf starten"]
        positions = [position(step, new) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(position("displayName: Zielprojekt, Person und Repo prüfen"), new)
        self.assertLess(position("displayName: Namen prüfen und eintragen"), new)

    def test_terraform_berechtigt_nicht_selbst(self):
        config = (embedded.ROOT / CONFIG).read_text(encoding="utf-8")
        self.assertIn('"authorize_service_connections": false', config)
        self.assertIn('"create_project": false', config)
        self.assertIn('"create_service_connection": true', config)
        self.assertIn('"requester_can_approve": ${{ lower(parameters.requesterCanApprove) }}', config)
        self.assertIn('"create_resource_groups": false', config)

    def test_zielrepo_nur_referenziert(self):
        index = position("- checkout: git://")
        self.assertIn("condition: false", SCAFFOLD[index:index + 300])
        self.assertNotIn("persistCredentials", SCAFFOLD)

    def test_pat_bricht_ab(self):
        self.assertIn("values: [jobToken, servicePrincipal, pat]", SCAFFOLD)
        self.assertIn("pat gibt es ab v0.20.0 nicht mehr", SCAFFOLD)
        self.assertNotIn("SeedScaffoldPat", SCAFFOLD)

    def test_versionen(self):
        for name, version in (("terraformModuleVersion", "v0.20.0"), ("pipelinesVersion", "v0.21.0"), ("templateRef", "v0.19.0")):
            with self.subTest(name=name):
                self.assertIn(f"  - name: {name}\n    type: string\n    default: {version}\n", SCAFFOLD)


@unittest.skipUnless(yaml, "PyYAML fehlt")
class InputTests(unittest.TestCase):
    """Eingaben der startenden Person nie als Text in einem Skript, nur in env."""

    INPUTS = ("parameters.name", "parameters.adoProject", "parameters.repository", "parameters.approvers",
              "parameters.environments", "parameters.templateRef", "parameters.templateUrl")

    def scripts(self, node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("bash", "pwsh", "script", "inlineScript") and isinstance(value, str):
                    yield value
                else:
                    yield from self.scripts(value)
        elif isinstance(node, list):
            for item in node:
                yield from self.scripts(item)

    def test_keine_eingaben_in_skripten(self):
        for path in ("templates/scaffold.yml", "templates/steps/scaffold-token.yml"):
            document = yaml.safe_load((embedded.ROOT / path).read_text(encoding="utf-8"))
            for script in self.scripts(document):
                for name in self.INPUTS:
                    self.assertNotIn(name, script, f"{path}: {name} im Skript")

    def test_yaml_lesbar(self):
        for path in ("templates/scaffold.yml", "templates/steps/scaffold-config.yml", "templates/steps/scaffold-token.yml",
                     "scaffold/central-pipelines.yml"):
            with self.subTest(path=path):
                self.assertIsNotNone(yaml.safe_load((embedded.ROOT / path).read_text(encoding="utf-8")))


class TfvarsTests(unittest.TestCase):
    def setUp(self):
        self.tfvars = embedded.load(CONFIG, "scaffold-tfvars")
        self.static = {"project": {"name": "kundenportal", "create_project": False}}
        self.env = {
            "SEED_PROJECT_NAME": "Projekt-0042", "SEED_REPOSITORY": "kundenportal", "SEED_TENANT_ID": "t",
            "SEED_SUBSCRIPTION_ID": "s", "SEED_SUBSCRIPTION_NAME": "Abo", "SEED_IDENTITY_CLIENT_ID": "c",
            "SEED_REQUESTER_EMAIL": "erika@example.org",
        }

    def test_werte_aus_der_umgebung(self):
        project = self.tfvars.merge(self.static, self.env)["project"]
        self.assertEqual(project["ado_project"], "Projekt-0042")
        self.assertEqual(project["repository"], "kundenportal")
        self.assertEqual(project["service_connection_identity"],
                         {"tenant_id": "t", "subscription_id": "s", "subscription_name": "Abo", "client_id": "c"})
        self.assertEqual(project["contributors"], ["erika@example.org"])
        self.assertFalse(project["create_project"])

    def test_fehlender_schritt(self):
        self.env["SEED_IDENTITY_CLIENT_ID"] = "$(seedIdentityClientId)"
        with self.assertRaisesRegex(ValueError, "SEED_IDENTITY_CLIENT_ID"):
            self.tfvars.merge(self.static, self.env)

    def test_ohne_mailadresse_keine_contributors(self):
        self.env["SEED_REQUESTER_EMAIL"] = "$(Build.RequestedForEmail)"
        self.assertEqual(self.tfvars.merge(self.static, self.env)["project"]["contributors"], [])


if __name__ == "__main__":
    unittest.main()
