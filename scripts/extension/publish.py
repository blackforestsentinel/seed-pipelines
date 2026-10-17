#!/usr/bin/env python3
"""Signiert eine Browser-Erweiterung als CRX3 und veröffentlicht sie im Projekt-Storage.

Teil von seed-pipelines: Der Deploy ruft das Skript mit extension.signing auf
(templates/steps/extension-package.yml). Eingabe ist das Paket <name>-<env>.zip aus demselben Deploy
(config.json der Umgebung, ohne key). Das Skript

  1. setzt update_url im Manifest auf <apiBaseUrl>/api/extension/updates.xml und entfernt key,
  2. packt das Zip deterministisch und signiert es als CRX3 mit dem privaten Schlüssel (PEM); die
     Erweiterungs-ID folgt aus dem Schlüssel und muss --expect-id sein,
  3. prüft Signatur und ID wie der Browser beim Installieren,
  4. lädt <name>-<version>.crx und danach current.json in den Container extension des
     Daten-Storage, außer der Inhalt ist derselbe wie beim letzten Mal (die Version zählt dabei
     nicht). Eine kleinere oder gleiche Version mit anderem Inhalt bricht ab (Downgrade-Schutz),
  5. fragt updates.xml und das CRX über die API ab (Bfs.Seed.Extension), bis sie die
     veröffentlichte Version ausliefert.

Ohne --storage-account und --resource-group nur Schritte 1 bis 3, etwa zum Ausprobieren. Braucht
Python 3.10 (nur Standardbibliothek) und openssl; zum Veröffentlichen az, angemeldet mit Storage Blob
Data Contributor auf dem Container (seed-terraform, Modul storage: extension_publisher_principal_id).

  python3 scripts/extension/publish.py --zip my-extension-prod.zip --key my-extension.pem \\
      --expect-id <id> --name my-extension --out out [--resource-group rg-<projekt>-prod]

Tests vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py" (braucht openssl)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from pathlib import Path

CONTAINER = "extension"
CURRENT = "current.json"
UPDATES_PATH = "/api/extension/updates.xml"
CRX_CONTENT_TYPE = "application/x-chrome-extension"
SIGNED_DATA_PREFIX = b"CRX3 SignedData\x00"
GUPDATE = "{http://www.google.com/update2/response}"
USER_AGENT = "sentinel-seed"
STORAGE_TAG = ("seed:module", "storage")

# Wie Bfs.Seed.Extension (SeedExtension.IsValidPackageName): Präfix der Dateinamen der Pakete.
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ID_PATTERN = re.compile(r"[a-p]{32}")
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]{0,4})(\.(0|[1-9][0-9]{0,4})){0,3}")

# Rollen wirken erst einige Minuten nach dem Apply, der sie vergibt. Bis dahin antwortet der
# Storage mit 403 AuthorizationPermissionMismatch; az ersetzt den Code durch einen eigenen Text.
PERMISSION_DENIED = (
    "AuthorizationPermissionMismatch",
    "You do not have the required permissions needed to perform this operation",
)
ROLE_WAIT_SECONDS = 600
ROLE_RETRY_SECONDS = 30
SERVE_ATTEMPTS = 12
SERVE_DELAY_SECONDS = 15

ENDPOINT_HINT = ("Ist AddSeedExtensionUpdates() aus Bfs.Seed.Extension eingebunden und der Container "
                 "extension in project.yaml (storage.containers)?")

# Für Tests austauschbar
clock = time.monotonic
sleep = time.sleep


class PublishError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.logissue type=warning]{message}", flush=True)
    else:
        print(f"Warnung: {message}", flush=True)


def fail(message: str) -> None:
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.logissue type=error]{message}", flush=True)
    raise PublishError(message)


# --- CRX3 -----------------------------------------------------------------------------------
# Aufbau wie in Chromium (components/crx_file): "Cr24", Version 3, Länge des Headers, Header als
# Protobuf CrxFileHeader, dann das Zip. Signiert wird "CRX3 SignedData\0" + Länge + SignedData +
# Zip mit RSA-SHA256 (PKCS#1 v1.5). SignedData enthält die ersten 16 Byte des SHA-256 über den
# öffentlichen Schlüssel (SPKI, DER); als a bis p geschrieben ist das die Erweiterungs-ID.


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _read_fields(data: bytes) -> list[tuple[int, bytes]]:
    """Nur längenbegrenzte Felder (Typ 2), mehr kommt in CRX-Headern nicht vor."""
    fields, pos = [], 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        if key & 7 != 2:
            raise PublishError(f"Unerwarteter Feldtyp {key & 7} im CRX-Header")
        length, pos = _read_varint(data, pos)
        fields.append((key >> 3, data[pos:pos + length]))
        pos += length
    return fields


def _openssl(args: list[str], data: bytes | None = None) -> bytes:
    # stderr kann Pfade nennen, aber nie den Schlüssel selbst; der geht nur als Datei an openssl.
    result = subprocess.run(["openssl", *args], input=data, capture_output=True)
    if result.returncode != 0:
        fail(f"openssl {args[0]}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def public_key_der(key_file: str) -> bytes:
    return _openssl(["pkey", "-in", key_file, "-pubout", "-outform", "DER"])


def crx_id(public_key: bytes) -> str:
    digest = hashlib.sha256(public_key).digest()[:16]
    return "".join(chr(97 + (b >> 4)) + chr(97 + (b & 15)) for b in digest)


def _signed_payload(signed_header_data: bytes, zip_bytes: bytes) -> bytes:
    return SIGNED_DATA_PREFIX + struct.pack("<I", len(signed_header_data)) + signed_header_data + zip_bytes


def pack_crx(zip_bytes: bytes, key_file: str) -> tuple[bytes, str]:
    public_key = public_key_der(key_file)
    signed_header_data = _field(1, hashlib.sha256(public_key).digest()[:16])
    signature = _openssl(["dgst", "-sha256", "-sign", key_file], _signed_payload(signed_header_data, zip_bytes))
    header = _field(2, _field(1, public_key) + _field(2, signature)) + _field(10000, signed_header_data)
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + zip_bytes, crx_id(public_key)


def verify_crx(crx: bytes) -> tuple[str, bytes]:
    """Prüft Format, ID und Signatur wie der Browser beim Installieren; liefert ID und Zip."""
    if crx[:4] != b"Cr24" or struct.unpack("<I", crx[4:8])[0] != 3:
        fail("Kein CRX3 (Kennung Cr24, Version 3 erwartet).")
    header_size = struct.unpack("<I", crx[8:12])[0]
    header, zip_bytes = crx[12:12 + header_size], crx[12 + header_size:]
    fields = _read_fields(header)
    signed_header_data = next((value for number, value in fields if number == 10000), None)
    proofs = [dict(_read_fields(value)) for number, value in fields if number == 2]
    if signed_header_data is None or len(proofs) != 1:
        fail("CRX-Header: genau ein RSA-Nachweis und SignedData erwartet.")
    public_key, signature = proofs[0].get(1, b""), proofs[0].get(2, b"")
    declared = dict(_read_fields(signed_header_data)).get(1, b"")
    if declared != hashlib.sha256(public_key).digest()[:16]:
        fail("CRX-Header: Die ID in SignedData passt nicht zum öffentlichen Schlüssel.")
    with tempfile.TemporaryDirectory() as work:
        key_file, signature_file = Path(work, "public.der"), Path(work, "signature")
        key_file.write_bytes(public_key)
        signature_file.write_bytes(signature)
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(key_file), "-keyform", "DER", "-signature", str(signature_file)],
            input=_signed_payload(signed_header_data, zip_bytes), capture_output=True)
        if result.returncode != 0:
            fail("CRX-Signatur ungültig.")
    return crx_id(public_key), zip_bytes


# --- Paket ----------------------------------------------------------------------------------


def read_zip(path: str) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}


def build_zip(files: dict[str, bytes]) -> bytes:
    """Gleicher Inhalt ergibt dasselbe Zip: feste Zeitstempel und Reihenfolge."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return buffer.getvalue()


def prepare(files: dict[str, bytes]) -> tuple[dict[str, bytes], dict, str]:
    """Manifest für das Selbst-Hosting: update_url aus config.json, ohne key."""
    if "manifest.json" not in files or "config.json" not in files:
        fail("Im Paket fehlen manifest.json oder config.json (Paket <name>-<umgebung>.zip aus dem Deploy?).")
    manifest = json.loads(files["manifest.json"])
    api = str(json.loads(files["config.json"]).get("apiBaseUrl", "")).rstrip("/")
    if not api.startswith("https://"):
        fail(f"config.json: apiBaseUrl „{api}“ ist keine https-Adresse.")
    version = manifest.get("version", "")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version) or max(map(int, version.split("."))) > 65535:
        fail(f"manifest.json: version ist „{version}“, erwartet bis zu vier Zahlen von 0 bis 65535 ohne führende Nullen.")
    manifest.pop("key", None)
    manifest["update_url"] = api + UPDATES_PATH
    prepared = dict(files)
    prepared["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return prepared, manifest, api


def content_hash(files: dict[str, bytes]) -> str:
    """SHA-256 über alle Dateien; die Version im Manifest zählt nicht, sie steigt mit jedem Lauf."""
    digest = hashlib.sha256()
    for name in sorted(files):
        data = files[name]
        if name == "manifest.json":
            manifest = json.loads(data)
            manifest.pop("version", None)
            data = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def parse_version(version: object) -> tuple[int, ...] | None:
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        return None
    parts = tuple(int(part) for part in version.split("."))
    # 1.2 und 1.2.0.0 sind für den Browser dieselbe Version.
    return parts + (0,) * (4 - len(parts))


def needs_publish(current: dict | None, release: dict) -> bool:
    """Entscheidet anhand von current.json, ob veröffentlicht wird; bricht bei einem Downgrade ab.

    Unverändert ist eine Veröffentlichung mit derselben ID, demselben Inhalt (contentHash, ohne
    Version) und derselben update_url, egal mit welcher Version. Sonst muss die Version steigen:
    Browser installieren keine kleinere Version, und eine gleiche mit anderem Inhalt überschriebe
    ein Paket, das Browser für immer zwischenspeichern dürfen.
    """
    if not current:
        return True
    if current.get("id") != release["id"]:
        warn(f"Die Erweiterungs-ID ändert sich von {current.get('id')} auf {release['id']} (anderer Schlüssel). "
             "Browser sehen eine neue Erweiterung; Intune-Richtlinie, Redirect-URI und CORS brauchen die neue ID.")
        return True
    if current.get("contentHash") == release["contentHash"] and current.get("updateUrl") == release["updateUrl"]:
        return False
    published, new = parse_version(current.get("version")), parse_version(release["version"])
    if published is None:
        warn(f"current.json nennt keine gültige Version („{current.get('version')}“); sie wird ersetzt.")
        return True
    if new < published:
        fail(f"Downgrade: Veröffentlicht ist {current.get('version')} (Build {current.get('buildId')}), dieser Lauf hat "
             f"{release['version']} mit anderem Inhalt. Browser installieren keine kleinere Version, und ein älterer Lauf "
             "soll die Erweiterung nicht zurücksetzen. Für einen alten Stand die Änderung zurücknehmen und neu bauen.")
    if new == published:
        fail(f"Version {release['version']} ist schon mit anderem Inhalt veröffentlicht. Ein Paket ändert sich unter "
             "derselben Version nie; die Version in manifest.json muss steigen.")
    return True


# --- Storage --------------------------------------------------------------------------------

AZ = shutil.which("az") or "az"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def az(args: list[str]) -> str:
    """az mit Wiederholung, solange der Storage die Datenrolle noch nicht kennt (403)."""
    deadline = clock() + ROLE_WAIT_SECONDS
    attempt = 1
    while True:
        result = _run([AZ, *args, "--only-show-errors"])
        if result.returncode == 0:
            return result.stdout.strip()
        error = result.stderr.strip()
        if not any(marker in error for marker in PERMISSION_DENIED):
            fail(f"az {' '.join(args[:3])}: {error}")
        if clock() + ROLE_RETRY_SECONDS > deadline:
            fail(f"Keine Datenrechte auf dem Container {CONTAINER} (403), auch nach {ROLE_WAIT_SECONDS // 60} Minuten nicht. "
                 "Die Deployment-Identität braucht Storage Blob Data Contributor auf dem Container: seed-terraform ab der "
                 "Version mit extension_publisher_principal_id im Modul storage, im Projekt infra/storage.tf wie in "
                 "seed-template, danach ein Apply.")
        log(f"Noch keine Datenrechte auf dem Container {CONTAINER} (403, Versuch {attempt}); eine neue Rolle wirkt erst "
            f"einige Minuten nach dem Apply. Neuer Versuch in {ROLE_RETRY_SECONDS} Sekunden.")
        sleep(ROLE_RETRY_SECONDS)
        attempt += 1


def find_storage_account(resource_group: str) -> str:
    """Der Daten-Storage aus dem Modul storage, erkannt am Tag seed:module=storage."""
    accounts = json.loads(az(["storage", "account", "list", "--resource-group", resource_group,
                              "--query", "[].{name:name, tags:tags}", "-o", "json"]) or "[]")
    found = [a["name"] for a in accounts if (a.get("tags") or {}).get(STORAGE_TAG[0]) == STORAGE_TAG[1]]
    if len(found) != 1:
        fail(f"Erwartet genau einen Storage Account mit dem Tag {STORAGE_TAG[0]}={STORAGE_TAG[1]} in {resource_group}, "
             f"gefunden: {len(found)}. Ist features.storage in project.yaml an und der Apply gelaufen?")
    return found[0]


class BlobContainer:
    def __init__(self, account: str, resource_group: str | None = None):
        self.account = account
        self.resource_group = resource_group

    def _blob(self, name: str) -> list[str]:
        return ["--account-name", self.account, "--container-name", CONTAINER, "--name", name, "--auth-mode", "login"]

    def check(self) -> None:
        # Über die Ressourcenverwaltung: Das braucht keine Datenrolle, die es ohne Container nicht gibt.
        group = ["--resource-group", self.resource_group] if self.resource_group else []
        exists = az(["storage", "container-rm", "exists", "--storage-account", self.account, *group, "--name", CONTAINER,
                     "--query", "exists", "-o", "tsv"])
        if exists != "true":
            fail(f"Container {CONTAINER} fehlt im Storage Account {self.account}. In project.yaml unter "
                 f"storage.containers eintragen; Terraform legt ihn mit dem nächsten Apply an.")

    def read_json(self, name: str, work: Path) -> dict | None:
        if az(["storage", "blob", "exists", *self._blob(name), "--query", "exists", "-o", "tsv"]) != "true":
            return None
        target = work / name
        az(["storage", "blob", "download", *self._blob(name), "--file", str(target), "-o", "none"])
        return json.loads(target.read_text(encoding="utf-8"))

    def upload(self, name: str, path: Path, content_type: str) -> None:
        az(["storage", "blob", "upload", *self._blob(name), "--file", str(path),
            "--content-type", content_type, "--overwrite", "true", "-o", "none"])


# --- Auslieferung ---------------------------------------------------------------------------


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def wait_until_served(release: dict, attempts: int = SERVE_ATTEMPTS, delay: float = SERVE_DELAY_SECONDS) -> None:
    """Jede Instanz der API hält current.json eine Minute; bis dahin kann die alte Version kommen."""
    url = release["updateUrl"]
    seen, answered = None, False
    for attempt in range(1, attempts + 1):
        answered = False
        try:
            response = ElementTree.fromstring(_get(url))
            answered = True
            app = response.find(f"{GUPDATE}app")
            check = None if app is None else app.find(f"{GUPDATE}updatecheck")
            seen = "kein updatecheck" if check is None else f"{app.get('appid')} {check.get('version')}"
            if check is not None and app.get("appid") == release["id"] and check.get("version") == release["version"]:
                codebase = check.get("codebase")
                try:
                    package = _get(codebase)
                except urllib.error.HTTPError as error:
                    fail(f"{codebase} antwortet mit {error.code}. Heißt das Paket anders, als die App erwartet? Der Präfix "
                         f"ist {release.get('name')}; ohne App-Setting Extension__PackageName nimmt Bfs.Seed.Extension den "
                         "Projektnamen.")
                if hashlib.sha256(package).hexdigest() != release["sha256"]:
                    fail(f"{codebase} liefert ein anderes CRX als veröffentlicht.")
                log(f"Ausgeliefert: {url} nennt {release['version']}, das CRX stimmt.")
                return
        except urllib.error.HTTPError as error:
            seen = f"HTTP {error.code}"
        except ElementTree.ParseError as error:
            seen = f"kein gültiges XML ({error})"
        except OSError as error:
            seen = f"nicht erreichbar ({getattr(error, 'reason', error)})"
        if attempt < attempts:
            sleep(delay)
    if not answered:
        fail(f"{url} liefert kein Update-Manifest (zuletzt: {seen}). {ENDPOINT_HINT}")
    fail(f"{url} nennt nach {attempts} Versuchen nicht {release['id']} {release['version']} (zuletzt: {seen}).")


# --- Ablauf ---------------------------------------------------------------------------------


def set_output(name: str, value: str) -> None:
    if os.environ.get("TF_BUILD"):
        print(f"##vso[task.setvariable variable={name};isOutput=true]{value}", flush=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--zip", required=True, help="Paket <name>-<umgebung>.zip aus dem Deploy (mit config.json)")
    parser.add_argument("--key", required=True, help="Privater RSA-Schlüssel (PEM), im Deploy aus der Secure File")
    parser.add_argument("--expect-id", required=True, help="Erwartete Erweiterungs-ID (extension.signing.id)")
    parser.add_argument("--name", required=True, help="Präfix der Paketnamen, wie extension.name: <name>-<version>.crx")
    parser.add_argument("--out", required=True, help="Ordner für CRX und release.json")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--resource-group", help="Resource Group der Umgebung; der Storage Account mit dem Tag seed:module=storage")
    target.add_argument("--storage-account", help="Storage Account mit dem Container extension")
    args = parser.parse_args(argv)

    if not NAME_PATTERN.fullmatch(args.name):
        fail(f"--name „{args.name}“: Kleinbuchstaben, Ziffern und Bindestriche, höchstens 63 Zeichen (wie Bfs.Seed.Extension).")
    if not ID_PATTERN.fullmatch(args.expect_id):
        fail(f"--expect-id „{args.expect_id}“: 32 Zeichen a bis p erwartet.")
    # Leer heißt im Deploy: Output resource_group_name fehlt. Dann nicht still nur packen.
    if args.resource_group == "" or args.storage_account == "":
        fail("--resource-group bzw. --storage-account ist leer (Output resource_group_name von Terraform?).")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    files, manifest, api = prepare(read_zip(args.zip))
    version = manifest["version"]
    crx, extension_id = pack_crx(build_zip(files), args.key)
    if extension_id != args.expect_id:
        fail(f"Der Schlüssel ergibt die ID {extension_id}, erwartet ist {args.expect_id} (extension.signing.id). "
             "Falsche Secure File oder falsche ID?")
    verified_id, _ = verify_crx(crx)
    if verified_id != extension_id:
        fail("Die Signaturprüfung ergibt eine andere ID.")

    file = f"{args.name}-{version}.crx"
    crx_path = out / file
    crx_path.write_bytes(crx)
    release = {
        "id": extension_id,
        "version": version,
        "file": file,
        "sha256": hashlib.sha256(crx).hexdigest(),
        "contentHash": content_hash(files),
        "updateUrl": manifest["update_url"],
        "apiBaseUrl": api,
        "name": args.name,
        "buildId": os.environ.get("BUILD_BUILDID"),
    }
    log(f"Gepackt: {file}, ID {extension_id}, {len(crx)} Bytes, update_url {release['updateUrl']}")

    published = False
    account = args.storage_account or (find_storage_account(args.resource_group) if args.resource_group else None)
    if account:
        container = BlobContainer(account, args.resource_group)
        container.check()
        with tempfile.TemporaryDirectory() as work:
            current = container.read_json(CURRENT, Path(work))
            if needs_publish(current, release):
                log(f"Veröffentliche {version} in {account}/{CONTAINER} (bisher {current.get('version') if current else 'keine'}).")
                # Erst das CRX, dann current.json: Bricht der zweite Schritt ab, bleibt die alte
                # Version aktiv, und ein neuer Lauf holt ihn nach.
                container.upload(file, crx_path, CRX_CONTENT_TYPE)
                current_file = Path(work, "upload-" + CURRENT)
                current_file.write_text(json.dumps(release, indent=2), encoding="utf-8")
                container.upload(CURRENT, current_file, "application/json")
                published = True
            else:
                log(f"Inhalt unverändert seit {current.get('version')} (Build {current.get('buildId')}); nichts zu veröffentlichen.")
                release = current
                crx_path.unlink()
        # Auch ohne neue Veröffentlichung: Ein Lauf, der nach dem Hochladen an der Auslieferung
        # scheiterte, ginge beim Neustart sonst still durch.
        wait_until_served(release)

    (out / "release.json").write_text(json.dumps({**release, "published": published}, indent=2), encoding="utf-8")
    set_output("published", str(published).lower())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except PublishError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)
