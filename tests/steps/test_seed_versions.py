"""Tests für den Hinweis auf veraltetes Seed (templates/steps/seed-versions.yml), nur Standardbibliothek.

Vom Repo-Root: python3 -m unittest discover -s tests/steps -p "test_*.py"
Ohne Netz: Tags, NuGet und npm kommen aus Attrappen.
"""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import embedded

TEMPLATE = "templates/steps/seed-versions.yml"
versions = embedded.load(TEMPLATE, "seed-versions")

ENV = {"SEED_PIPELINES_REF": "refs/tags/v0.15.0", "SEED_PIPELINES_REPOSITORY": "org/seed-pipelines"}
TAGS = {
    "https://github.com/org/seed-pipelines.git": ["v0.9.0", "v0.15.0", "v0.16.0", "v0.17.0-rc.1"],
    "https://github.com/org/seed-terraform.git": ["v0.15.0", "v0.16.0"],
}
FEEDS = {
    "https://api.nuget.org/v3-flatcontainer/bfs.seed.auth/index.json": {"versions": ["0.8.0", "0.9.0", "0.10.0-preview.1"]},
    "https://api.nuget.org/v3-flatcontainer/bfs.seed.mcp/index.json": {"versions": ["0.9.0"]},
    "https://registry.npmjs.org/@blackforestsentinel%2Fseed-web-core": {"versions": {"0.8.0": {}, "0.9.0": {}}},
}

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <ItemGroup>
    <PackageReference Include="Bfs.Seed.Auth" Version="0.8.0" />
    <PackageReference Include="Bfs.Seed.Mcp">
      <Version>0.9.0</Version>
    </PackageReference>
    <PackageReference Include="Other.Package" Version="1.0.0" />
  </ItemGroup>
</Project>
"""
MAIN_TF = """module "core" {
  source = "git::https://github.com/org/seed-terraform.git//core?ref=v0.15.0"
}
module "sso" {
  source = "git::https://github.com/org/seed-terraform.git//sso?ref=v0.15.0"
}
module "other" {
  source = "git::https://github.com/org/anderes-modul.git//x?ref=v0.1.0"
}
"""


def fake_tags(url):
    if url not in TAGS:
        raise OSError("nicht erreichbar")
    return TAGS[url]


def fake_fetch(url, accept="application/json"):
    if url not in FEEDS:
        raise OSError("nicht erreichbar")
    return FEEDS[url]


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)

    def tearDown(self):
        self.work.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_main(self, env=ENV, tags=fake_tags, fetch=fake_fetch):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = versions.main(str(self.root), "infra", env=env, tags=tags, fetch=fetch)
        self.assertEqual(code, 0)
        text = out.getvalue()
        return [line for line in text.splitlines() if line.startswith("##vso[")], text

    def test_releases_only(self):
        self.assertEqual(versions.release("v1.2.3"), (1, 2, 3))
        self.assertEqual(versions.release("0.10.0"), (0, 10, 0))
        for text in ("0.10.0-preview.1", "main", "1.2", "$(SeedVersion)", "0.9.*"):
            self.assertIsNone(versions.release(text), text)
        self.assertEqual(versions.newest(["0.9.0", "0.10.0-preview.1", "0.10.0", "v0.2.0"]), (0, 10, 0))

    def test_reads_modules_packages_and_lockfile(self):
        self.write("infra/main.tf", MAIN_TF)
        self.write("infra/.terraform/modules/core/main.tf", 'source = "git::https://github.com/org/seed-terraform.git//x?ref=v0.1.0"')
        self.write("api/Api/Api.csproj", CSPROJ)
        self.write("api/Api/obj/Api.csproj.nuget.g.props", CSPROJ.replace("0.8.0", "0.1.0"))
        self.write("Directory.Packages.props", '<Project><ItemGroup><PackageVersion Include="Bfs.Seed.Storage" Version="[0.9.0]" /></ItemGroup></Project>')
        self.write("frontend/package.json", json.dumps({"dependencies": {"@blackforestsentinel/seed-web-core": "^0.8.0", "react": "19.0.0"}}))
        self.write("frontend/package-lock.json", json.dumps({"packages": {"node_modules/@blackforestsentinel/seed-web-core": {"version": "0.8.1"}}}))
        self.write("extension/package.json", json.dumps({"devDependencies": {"@blackforestsentinel/seed-web-auth": "~0.9.0"}}))

        modules = versions.module_refs(self.root / "infra")
        self.assertEqual(list(modules), ["org"])
        self.assertEqual(sorted(modules["org"]["v0.15.0"]), [f"{(self.root / 'infra/main.tf').as_posix()}:{n}" for n in (2, 5)])
        self.assertEqual(versions.nuget_refs(self.root), {"Bfs.Seed.Auth": {"0.8.0"}, "Bfs.Seed.Mcp": {"0.9.0"}, "Bfs.Seed.Storage": {"0.9.0"}})
        self.assertEqual(versions.npm_refs(self.root), {"@blackforestsentinel/seed-web-core": {"0.8.1"}, "@blackforestsentinel/seed-web-auth": {"0.9.0"}})

    def test_warns_per_source_when_behind(self):
        self.write("infra/main.tf", MAIN_TF)
        self.write("api/Api/Api.csproj", CSPROJ)
        self.write("frontend/package.json", json.dumps({"dependencies": {"@blackforestsentinel/seed-web-core": "0.8.0"}}))
        issues, text = self.run_main()
        self.assertEqual(len(issues), 4)
        self.assertTrue(all(i.startswith("##vso[task.logissue type=warning]") for i in issues))
        self.assertIn("org/seed-pipelines v0.15.0 (neu: v0.16.0)", issues[0])  # rc zählt nicht
        self.assertIn("org/seed-terraform v0.15.0 (neu: v0.16.0)", issues[1])
        self.assertIn("Bfs.Seed.Auth 0.8.0 (neu: 0.9.0)", issues[2])        # preview zählt nicht
        self.assertNotIn("Bfs.Seed.Mcp", issues[2])
        self.assertIn("@blackforestsentinel/seed-web-core 0.8.0 (neu: 0.9.0)", issues[3])
        self.assertIn("aktuell: Bfs.Seed.Mcp 0.9.0", text)

    def test_msbuild_property_is_resolved(self):
        self.write("Directory.Build.props", "<Project><PropertyGroup><SeedVersion>0.8.0</SeedVersion></PropertyGroup></Project>")
        self.write("api/Api/Api.csproj", CSPROJ.replace('Version="0.8.0"', 'Version="$(SeedVersion)"'))
        self.assertEqual(versions.nuget_refs(self.root), {"Bfs.Seed.Auth": {"0.8.0"}, "Bfs.Seed.Mcp": {"0.9.0"}})
        issues, _ = self.run_main(env={})
        self.assertEqual(len(issues), 1)
        self.assertIn("Bfs.Seed.Auth 0.8.0 (neu: 0.9.0)", issues[0])

    def test_unclear_package_versions_warn(self):
        # Property ohne eindeutigen Wert, unbekannte Property, Bereich: nicht still übergehen
        self.write("Directory.Build.props", "<Project><PropertyGroup><SeedVersion>0.8.0</SeedVersion></PropertyGroup>"
                                            "<PropertyGroup Condition=\"'$(X)' == 'y'\"><SeedVersion>0.9.0</SeedVersion></PropertyGroup></Project>")
        self.write("api/Api/Api.csproj", CSPROJ.replace('Version="0.8.0"', 'Version="$(SeedVersion)"').replace(
            "<Version>0.9.0</Version>", "<Version>$(Unbekannt)</Version>"))
        self.write("api/Other/Other.csproj", '<Project><ItemGroup><PackageReference Include="Bfs.Seed.Storage" Version="0.9.*" /></ItemGroup></Project>')
        self.write("frontend/package.json", json.dumps({"dependencies": {"@blackforestsentinel/seed-web-core": "file:../seed-web-core"}}))

        def offline(*args, **kwargs):
            raise OSError("Netz weg")

        issues, _ = self.run_main(env={}, tags=offline, fetch=offline)
        self.assertEqual(len(issues), 2)
        self.assertIn("NuGet-Pakete Bfs.Seed.*: Version nicht prüfbar: Bfs.Seed.Auth $(SeedVersion), "
                      "Bfs.Seed.Mcp $(Unbekannt), Bfs.Seed.Storage 0.9.*", issues[0])
        self.assertIn("npm-Pakete seed-web-*: Version nicht prüfbar: @blackforestsentinel/seed-web-core file:../seed-web-core", issues[1])

    def test_branch_refs_are_only_logged(self):
        self.write("infra/main.tf", MAIN_TF.replace("?ref=v0.15.0", "?ref=main"))
        issues, text = self.run_main(env={})
        self.assertEqual(issues, [])
        self.assertIn("keine feste Release-Version, kein Vergleich: org/seed-terraform main", text)

    def test_up_to_date_is_quiet(self):
        self.write("infra/main.tf", MAIN_TF.replace("v0.15.0", "v0.16.0"))
        issues, _ = self.run_main(env={"SEED_PIPELINES_REF": "refs/tags/v0.16.0", "SEED_PIPELINES_REPOSITORY": "org/seed-pipelines"})
        self.assertEqual(issues, [])

    def test_network_errors_are_log_lines_only(self):
        self.write("infra/main.tf", MAIN_TF)
        self.write("api/Api/Api.csproj", CSPROJ)

        def offline(*args, **kwargs):
            raise OSError("Netz weg")

        issues, text = self.run_main(tags=offline, fetch=offline)
        self.assertEqual(issues, [])
        self.assertIn("nicht abrufbar (OSError: Netz weg), übersprungen", text)
        # eine Zeile je Quelle (seed-pipelines, seed-terraform, NuGet), nicht je Paket
        self.assertEqual(text.count("nicht abrufbar"), 3)

    def test_branches_and_missing_resource_are_not_compared(self):
        for env, expected in (({"SEED_PIPELINES_REF": "refs/heads/main", "SEED_PIPELINES_REPOSITORY": "org/seed-pipelines"}, "kein Release-Tag"),
                              ({"SEED_PIPELINES_REF": "$(seedPipelinesRef)", "SEED_PIPELINES_REPOSITORY": "$(seedPipelinesRepository)"}, "nicht ermittelt"),
                              ({}, "nicht ermittelt")):
            with self.subTest(env=env):
                issues, text = self.run_main(env=env)
                self.assertEqual(issues, [])
                self.assertIn(expected, text)

    def test_script_never_fails_the_build(self):
        # Wie in der Pipeline: eigener Prozess; ein Fehler im Skript ergibt nur eine Zeile.
        script = self.root / "versions.py"
        script.write_text(embedded.source(TEMPLATE, "seed-versions").replace(
            "def main(root, infra,", "def main(root, infra, *_, **__):\n    raise RuntimeError('kaputt')\n\ndef _unused(root, infra,"), encoding="utf-8")
        result = subprocess.run([sys.executable, "-I", "-X", "utf8", str(script), ".", "infra"], cwd=self.root,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Prüfung übersprungen (RuntimeError: kaputt)", result.stdout)

    def test_logging_commands_from_repo_are_defused(self):
        self.write("frontend/package.json", json.dumps({"dependencies": {"@blackforestsentinel/seed-web-core": "##vso[task.complete result=Failed]"}}))
        _, text = self.run_main()
        self.assertNotIn("##vso[task.complete", text)


if __name__ == "__main__":
    unittest.main()
