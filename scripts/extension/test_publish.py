"""Tests für publish.py. Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py" (braucht openssl)

az und HTTP sind durch Fakes ersetzt; nur openssl läuft wirklich, mit einem Wegwerf-Schlüssel.
"""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

# Laden über den Pfad wie in scripts/intune: Bei der Suche ab scripts/ heißt das Paket dieses
# Ordners schon „extension“, und publish.py liegt nicht auf sys.path.
_spec = importlib.util.spec_from_file_location("seed_extension_publish", Path(__file__).with_name("publish.py"))
publish = importlib.util.module_from_spec(_spec)
sys.modules["seed_extension_publish"] = publish
_spec.loader.exec_module(publish)

# Das Skript meldet Fehler in Azure Pipelines zusätzlich als Annotation (##vso[task.logissue]).
# Hier sind Fehler gewollt; ohne TF_BUILD erscheinen sie nicht als Fehler auf der Seite des Laufs.
os.environ.pop("TF_BUILD", None)

API = "https://func-my-app-prod-abc123.azurewebsites.net"
UPDATE_URL = API + "/api/extension/updates.xml"
DENIED = ("ERROR: \nYou do not have the required permissions needed to perform this operation.\n"
          "Depending on your operation, you may need to be assigned one of the following roles: ...")


def package(manifest: dict, api: str = API, page: bytes = b"<!doctype html>") -> dict[str, bytes]:
    return {
        "manifest.json": json.dumps(manifest).encode(),
        "config.json": json.dumps({"apiBaseUrl": api}).encode(),
        "popup.html": page,
    }


def write_zip(directory: str, files: dict[str, bytes], name: str = "my-ext-prod.zip") -> str:
    path = Path(directory, name)
    with zipfile.ZipFile(path, "w") as archive:
        for file, data in files.items():
            archive.writestr(file, data)
    return str(path)


def quiet():
    """Ausgaben des Skripts (log, warn) aus der Testausgabe heraushalten."""
    return mock.patch("sys.stdout", new_callable=io.StringIO)


class FakeAzure:
    """az (Storage Account, Container, Blobs) und die API (updates.xml, Pakete) im Speicher."""

    def __init__(self, accounts=None, container=True, denied=0, serve=True):
        self.accounts = [{"name": "stmyappproddata123456", "tags": {"seed:module": "storage"}},
                         {"name": "stmyappprodabc123", "tags": {"seed:module": "core"}}] if accounts is None else accounts
        self.container = container
        self.denied = denied
        self.serve = serve
        self.blobs: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.commands: list[list[str]] = []

    @staticmethod
    def _value(args, option):
        return args[args.index(option) + 1]

    def run(self, args):
        args = args[1:]
        self.commands.append(args)
        ok = lambda out="": subprocess.CompletedProcess(args, 0, out, "")
        if args[:3] == ["storage", "account", "list"]:
            return ok(json.dumps(self.accounts))
        if args[:3] == ["storage", "container-rm", "exists"]:
            return ok("true" if self.container else "false")
        if args[:2] == ["storage", "blob"]:
            if self.denied:
                self.denied -= 1
                return subprocess.CompletedProcess(args, 1, "", DENIED)
            name = self._value(args, "--name")
            if args[2] == "exists":
                return ok("true" if name in self.blobs else "false")
            if args[2] == "download":
                Path(self._value(args, "--file")).write_bytes(self.blobs[name])
                return ok()
            if args[2] == "upload":
                self.blobs[name] = Path(self._value(args, "--file")).read_bytes()
                self.uploads.append(name)
                return ok()
        return subprocess.CompletedProcess(args, 2, "", f"unbekannt: {args}")

    def get(self, url):
        if not self.serve or "current.json" not in self.blobs:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        current = json.loads(self.blobs["current.json"])
        if url == UPDATE_URL:
            return (f"<?xml version='1.0' encoding='utf-8'?><gupdate xmlns='http://www.google.com/update2/response' "
                    f"protocol='2.0'><app appid='{current['id']}'><updatecheck codebase='{API}/api/extension/packages/"
                    f"{current['file']}' version='{current['version']}' hash_sha256='{current['sha256']}'/></app>"
                    f"</gupdate>").encode()
        file = url.rsplit("/", 1)[1]
        if file in self.blobs:
            return self.blobs[file]
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class KeyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = cls.new_key("key.pem")
        cls.id = publish.crx_id(publish.public_key_der(cls.key))

    @classmethod
    def new_key(cls, name):
        path = str(Path(cls.keys.name, name))
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", path],
                       check=True, capture_output=True)
        return path

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()


class CrxTests(KeyTestCase):
    def test_pack_and_verify_roundtrip(self):
        zip_bytes = publish.build_zip(package({"manifest_version": 3, "version": "1.0.0.1"}))

        crx, extension_id = publish.pack_crx(zip_bytes, self.key)

        self.assertEqual(crx[:4], b"Cr24")
        self.assertEqual(publish.verify_crx(crx), (extension_id, zip_bytes))
        self.assertEqual(extension_id, self.id)
        self.assertRegex(extension_id, "^[a-p]{32}$")

    def test_id_matches_sha256_of_public_key(self):
        der = subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-outform", "DER"],
                             check=True, capture_output=True).stdout
        digest = hashlib.sha256(der).hexdigest()[:32]

        self.assertEqual(self.id, digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop")))

    def test_verify_rejects_tampered_content(self):
        crx, _ = publish.pack_crx(publish.build_zip(package({"version": "1.0.0.1"})), self.key)
        tampered = crx[:-1] + bytes([crx[-1] ^ 1])

        with self.assertRaises(publish.PublishError):
            publish.verify_crx(tampered)

    def test_build_zip_is_deterministic(self):
        files = package({"version": "1.0.0.1"})

        self.assertEqual(publish.build_zip(files), publish.build_zip(dict(reversed(list(files.items())))))


class PackageTests(unittest.TestCase):
    def test_prepare_sets_update_url_and_drops_key(self):
        files, manifest, api = publish.prepare(package({"version": "1.1.0.2190", "key": "MIIB..."}))

        self.assertEqual(manifest["update_url"], UPDATE_URL)
        self.assertNotIn("key", json.loads(files["manifest.json"]))
        self.assertEqual(api, API)

    def test_prepare_rejects_http_api(self):
        with self.assertRaises(publish.PublishError):
            publish.prepare(package({"version": "1"}, api="http://localhost:7071"))

    def test_prepare_rejects_invalid_version(self):
        for version in ("1.02", "1.2.3.4.5", "70000", ""):
            with self.subTest(version=version), self.assertRaises(publish.PublishError):
                publish.prepare(package({"version": version}))

    def test_content_hash_ignores_version_only(self):
        first, _, _ = publish.prepare(package({"version": "1.1.0.2190"}))
        next_build, _, _ = publish.prepare(package({"version": "1.1.0.2191"}))
        changed = dict(next_build, **{"popup.html": b"<!doctype html><p>neu"})

        self.assertEqual(publish.content_hash(first), publish.content_hash(next_build))
        self.assertNotEqual(publish.content_hash(first), publish.content_hash(changed))

    def test_read_zip_skips_directories(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("assets/", b"")
            archive.writestr("assets/a.js", b"x")
        with tempfile.TemporaryDirectory() as work:
            path = Path(work, "p.zip")
            path.write_bytes(buffer.getvalue())

            self.assertEqual(publish.read_zip(str(path)), {"assets/a.js": b"x"})


class DecisionTests(unittest.TestCase):
    current = {"id": "a" * 32, "version": "1.2.0.200", "contentHash": "h1", "updateUrl": UPDATE_URL, "buildId": "200"}

    def release(self, **changes):
        return {**self.current, **changes}

    def test_first_release_is_published(self):
        self.assertTrue(publish.needs_publish(None, self.release()))

    def test_same_content_is_unchanged_whatever_the_version(self):
        for version in ("1.2.0.300", "1.2.0.200", "1.2.0.100"):
            with self.subTest(version=version):
                self.assertFalse(publish.needs_publish(self.current, self.release(version=version)))

    def test_new_content_with_higher_version_is_published(self):
        self.assertTrue(publish.needs_publish(self.current, self.release(version="1.2.0.201", contentHash="h2")))
        self.assertTrue(publish.needs_publish(self.current, self.release(version="1.3", contentHash="h2")))

    def test_downgrade_with_other_content_fails(self):
        with self.assertRaisesRegex(publish.PublishError, "Downgrade"):
            publish.needs_publish(self.current, self.release(version="1.2.0.199", contentHash="h2"))

    def test_same_version_with_other_content_fails(self):
        with self.assertRaisesRegex(publish.PublishError, "schon mit anderem Inhalt"):
            publish.needs_publish(self.current, self.release(contentHash="h2"))

    def test_versions_compare_numerically_and_padded(self):
        self.assertTrue(publish.needs_publish(self.release(version="1.2.0.9"), self.release(version="1.2.0.10", contentHash="h2")))
        with self.assertRaisesRegex(publish.PublishError, "schon mit anderem Inhalt"):
            publish.needs_publish(self.release(version="1.2"), self.release(version="1.2.0.0", contentHash="h2"))

    def test_other_update_url_counts_as_change(self):
        self.assertTrue(publish.needs_publish(self.current, self.release(version="1.2.0.201", updateUrl="https://x/api/extension/updates.xml")))

    def test_new_key_is_published_with_warning(self):
        with quiet() as output:
            self.assertTrue(publish.needs_publish(self.current, self.release(id="b" * 32, version="1.0.0.1")))

        self.assertIn("ID ändert sich", output.getvalue())


class AzTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0

        def advance(seconds):
            self.now += seconds

        patches = [mock.patch.object(publish, "clock", lambda: self.now), mock.patch.object(publish, "sleep", advance)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_retries_while_role_is_not_effective(self):
        azure = FakeAzure(denied=3)
        with mock.patch.object(publish, "_run", azure.run), quiet():
            result = publish.az(["storage", "blob", "exists", "--name", "current.json"])

        self.assertEqual(result, "false")
        self.assertEqual(self.now, 3 * publish.ROLE_RETRY_SECONDS)

    def test_gives_up_with_hint_after_wait(self):
        azure = FakeAzure(denied=1000)
        with mock.patch.object(publish, "_run", azure.run), quiet(), \
                self.assertRaisesRegex(publish.PublishError, "extension_publisher_principal_id"):
            publish.az(["storage", "blob", "exists", "--name", "current.json"])

        self.assertLessEqual(self.now, publish.ROLE_WAIT_SECONDS)

    def test_other_errors_fail_at_once(self):
        def broken(args):
            return subprocess.CompletedProcess(args, 1, "", "ERROR: The specified container does not exist.")

        with mock.patch.object(publish, "_run", broken), self.assertRaisesRegex(publish.PublishError, "does not exist"):
            publish.az(["storage", "blob", "exists"])

        self.assertEqual(self.now, 0)

    def test_finds_storage_account_by_tag(self):
        with mock.patch.object(publish, "_run", FakeAzure().run):
            self.assertEqual(publish.find_storage_account("rg-my-app-prod"), "stmyappproddata123456")

    def test_storage_account_must_be_unique(self):
        for accounts in ([], [{"name": "a", "tags": {"seed:module": "storage"}}, {"name": "b", "tags": {"seed:module": "storage"}}],
                         [{"name": "c", "tags": None}]):
            with self.subTest(accounts=accounts), mock.patch.object(publish, "_run", FakeAzure(accounts=accounts).run), \
                    self.assertRaisesRegex(publish.PublishError, "features.storage"):
                publish.find_storage_account("rg-my-app-prod")


class PublishTests(KeyTestCase):
    """main() von Anfang bis Ende gegen FakeAzure."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.azure = FakeAzure()
        for patch in (mock.patch.object(publish, "_run", self.azure.run), mock.patch.object(publish, "_get", self.azure.get),
                      mock.patch.object(publish, "sleep", lambda seconds: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def run_main(self, version, page=b"<!doctype html>", out="out", key=None, expect_id=None, target=("--resource-group", "rg-my-app-prod")):
        archive = write_zip(self.work.name, package({"manifest_version": 3, "version": version, "key": "MIIB..."}, page=page))
        out_dir = Path(self.work.name, out)
        with quiet() as output:
            publish.main(["--zip", archive, "--key", key or self.key, "--expect-id", expect_id or self.id,
                          "--name", "my-ext", "--out", str(out_dir), *target])
        self.output = output.getvalue()
        return out_dir, json.loads((out_dir / "release.json").read_text(encoding="utf-8"))

    def test_first_release_uploads_crx_before_current(self):
        out, release = self.run_main("1.0.0.100")

        self.assertEqual(self.azure.uploads, ["my-ext-1.0.0.100.crx", "current.json"])
        self.assertTrue(release["published"])
        self.assertEqual((release["id"], release["version"], release["file"], release["updateUrl"]),
                         (self.id, "1.0.0.100", "my-ext-1.0.0.100.crx", UPDATE_URL))
        current = json.loads(self.azure.blobs["current.json"])
        self.assertEqual({k: current[k] for k in ("id", "version", "file", "sha256")},
                         {k: release[k] for k in ("id", "version", "file", "sha256")})
        self.assertNotIn("published", current)
        crx = (out / "my-ext-1.0.0.100.crx").read_bytes()
        self.assertEqual(crx, self.azure.blobs["my-ext-1.0.0.100.crx"])
        _, zip_bytes = publish.verify_crx(crx)
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(zip_bytes)).read("manifest.json"))
        self.assertEqual(manifest["update_url"], UPDATE_URL)
        self.assertNotIn("key", manifest)

    def test_unchanged_content_is_not_published_again(self):
        self.run_main("1.0.0.100")

        out, release = self.run_main("1.0.0.101", out="out2")

        self.assertEqual(self.azure.uploads, ["my-ext-1.0.0.100.crx", "current.json"])
        self.assertFalse(release["published"])
        self.assertEqual(release["version"], "1.0.0.100")
        self.assertEqual([p.name for p in out.iterdir()], ["release.json"])
        self.assertIn("unverändert", self.output)

    def test_new_content_is_published(self):
        self.run_main("1.0.0.100")

        _, release = self.run_main("1.0.0.101", page=b"<p>neu", out="out2")

        self.assertEqual(self.azure.uploads[2:], ["my-ext-1.0.0.101.crx", "current.json"])
        self.assertEqual(json.loads(self.azure.blobs["current.json"])["version"], "1.0.0.101")
        self.assertTrue(release["published"])

    def test_older_build_does_not_downgrade(self):
        self.run_main("1.0.0.100")

        with self.assertRaisesRegex(publish.PublishError, "Downgrade"):
            self.run_main("1.0.0.99", page=b"<p>alt", out="out2")

        self.assertEqual(self.azure.uploads, ["my-ext-1.0.0.100.crx", "current.json"])

    def test_waits_for_role_on_first_publish(self):
        self.azure.denied = 2

        _, release = self.run_main("1.0.0.100")

        self.assertTrue(release["published"])
        self.assertIn("Noch keine Datenrechte", self.output)

    def test_wrong_key_fails_before_upload(self):
        with self.assertRaisesRegex(publish.PublishError, "erwartet ist"):
            self.run_main("1.0.0.100", expect_id="p" * 32)

        self.assertEqual(self.azure.commands, [])

    def test_missing_container(self):
        self.azure.container = False

        with self.assertRaisesRegex(publish.PublishError, "storage.containers"):
            self.run_main("1.0.0.100")

        self.assertEqual(self.azure.uploads, [])

    def test_endpoint_missing(self):
        self.azure.serve = False

        with self.assertRaisesRegex(publish.PublishError, r"AddSeedExtensionUpdates\(\)"):
            self.run_main("1.0.0.100")

    def test_rerun_after_failed_delivery_checks_again(self):
        self.azure.serve = False
        with self.assertRaises(publish.PublishError):
            self.run_main("1.0.0.100")

        with self.assertRaisesRegex(publish.PublishError, r"AddSeedExtensionUpdates\(\)"):
            self.run_main("1.0.0.100", out="out2")

    def test_served_package_must_match(self):
        original = self.azure.get
        self.azure.get = lambda url: b"anderes Paket" if url.endswith(".crx") else original(url)
        with mock.patch.object(publish, "_get", self.azure.get), \
                self.assertRaisesRegex(publish.PublishError, "anderes CRX"):
            self.run_main("1.0.0.100")

    def test_package_name_mismatch(self):
        original = self.azure.get

        def get(url):
            if url.endswith(".crx"):
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return original(url)

        with mock.patch.object(publish, "_get", get), self.assertRaisesRegex(publish.PublishError, "Extension__PackageName"):
            self.run_main("1.0.0.100")

    def test_without_storage_only_packs(self):
        out, release = self.run_main("1.0.0.100", target=())

        self.assertEqual(self.azure.commands, [])
        self.assertFalse(release["published"])
        self.assertTrue((out / "my-ext-1.0.0.100.crx").exists())

    def test_invalid_arguments(self):
        for argv in (["--name", "My_Ext", "--expect-id", "a" * 32], ["--name", "my-ext", "--expect-id", "q" * 32],
                     # Im Deploy leer, wenn der Output resource_group_name fehlt: nicht still nur packen
                     ["--name", "my-ext", "--expect-id", "a" * 32, "--resource-group", ""]):
            with self.subTest(argv=argv), self.assertRaises(publish.PublishError):
                publish.main(["--zip", "x.zip", "--key", self.key, "--out", self.work.name, *argv])

        self.assertEqual(self.azure.commands, [])


if __name__ == "__main__":
    unittest.main()
