"""Tests für intune.py, nur Standardbibliothek.

Vom Repo-Root: python3 -m unittest discover -s scripts -p "test_*.py"
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

# Laden über den Pfad: Bei der Suche ab scripts/ heißt das Paket dieses Ordners schon „intune“.
_spec = importlib.util.spec_from_file_location("seed_intune", Path(__file__).with_name("intune.py"))
intune = importlib.util.module_from_spec(_spec)
sys.modules["seed_intune"] = intune
_spec.loader.exec_module(intune)

try:
    import yaml  # nur zum Abgleich, wo PyYAML installiert ist
except ImportError:
    yaml = None

# Fehler sind hier gewollt; ohne TF_BUILD erscheinen sie nicht als Annotation im Lauf.
os.environ.pop("TF_BUILD", None)

LINK_ID = "flpkgokniclbiocmpjpmficmfcdghfgd"
LINK_URL = "https://func-sentinel-link-prod-f08084.azurewebsites.net/api/extension/updates.xml"
OTHER_ID = "abcdefghijklmnopabcdefghijklmnop"
OTHER_URL = "https://func-demo-prod.azurewebsites.net/api/extension/updates.xml"
LINK = f"{LINK_ID};{LINK_URL}"
OTHER = f"{OTHER_ID};{OTHER_URL}"
IDS = ("device_vendor_msft_policy_config_microsoft_edgev77.3~policy~microsoft_edge~extensions_extensioninstallforcelist",
       "device_vendor_msft_policy_config_microsoft_edgev77.3~policy~microsoft_edge~extensions_extensioninstallforcelist_1",
       "device_vendor_msft_policy_config_microsoft_edgev77.3~policy~microsoft_edge~extensions_extensioninstallforcelist_extensioninstallforcelistdesc")
DEFINITION = {
    "id": IDS[0],
    "options": [
        {"itemId": IDS[0] + "_0", "dependedOnBy": []},
        {"itemId": IDS[1], "dependedOnBy": [{"dependedOnBy": IDS[2], "required": True}]},
    ],
}
NAME = intune.POLICY_NAME
OLD = "Sentinel Link: Edge-Erweiterung"

REGISTRY = f"""\
# Edge-Erweiterungen dieses Tenants
extensions:
  - name: sentinel-link            # Projekt
    id: {LINK_ID}
    updateUrl: {LINK_URL}
    owner: ian.winter@blackforestsentinel.de
    comment: 'Startseite: Kacheln # und Suche'

  - name: demo
    id: "{OTHER_ID}"
    updateUrl: {OTHER_URL}
"""


def manifest(appid=LINK_ID, codebase="https://example.org/a.crx", version="1.0.0.1", ns=True):
    xmlns = ' xmlns="http://www.google.com/update2/response"' if ns else ""
    check = f'<updatecheck codebase="{codebase}" version="{version}"/>' if codebase else ""
    return f'<?xml version="1.0"?><gupdate protocol="2.0"{xmlns}><app appid="{appid}">{check}</app></gupdate>'.encode()


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()) as out:
        yield out


def settings_for(entries, enabled=True):
    ids = IDS if enabled else (IDS[0], IDS[0] + "_0", IDS[2])
    return intune.desired_policy(ids, entries)["settings"]


class FakeGraph:
    """Graph im Speicher, genug für read_state, find_conflicts und apply."""

    def __init__(self, definition=DEFINITION):
        self.definition = definition
        self.policies = {}
        self.calls = []
        self.next_id = 1

    def add(self, name, entries=None, settings=None, assignments=None, platforms="windows10", technologies="mdm"):
        policy_id = f"p{self.next_id}"
        self.next_id += 1
        self.policies[policy_id] = {
            "id": policy_id, "name": name, "platforms": platforms, "technologies": technologies,
            "settings": settings if settings is not None else settings_for(entries or []),
            "assignments": assignments if assignments is not None else [{"target": {"@odata.type": intune.ALL_USERS}}],
        }
        return policy_id

    def writes(self):
        return [(m, p) for m, p in self.calls if m != "GET"]

    def request(self, method, path, body=None, missing_ok=False):
        self.calls.append((method, path))
        if path.startswith("/deviceManagement/configurationSettings("):
            return self.definition if IDS[0] in path.replace("%7E", "~") else None
        match = re.fullmatch(r"/deviceManagement/configurationPolicies\('([^']+)'\)(/assign)?", path)
        if method == "POST" and path == "/deviceManagement/configurationPolicies":
            policy_id = self.add(body["name"], settings=body["settings"], assignments=[],
                                 platforms=body["platforms"], technologies=body["technologies"])
            return {"id": policy_id, "name": body["name"]}
        if method == "PUT" and match:
            policy = self.policies[match[1]]
            policy.update(name=body["name"], settings=body["settings"])
            return {}
        if method == "POST" and match and match[2]:
            self.policies[match[1]]["assignments"] = [{"target": a["target"]} for a in body["assignments"]]
            return {}
        if method == "DELETE" and match:
            del self.policies[match[1]]
            return {}
        raise AssertionError(f"unerwartet: {method} {path}")

    def pages(self, path):
        self.calls.append(("GET", path))
        if path.startswith("/deviceManagement/configurationPolicies?"):
            return [{k: p[k] for k in ("id", "name", "platforms", "technologies")} for p in self.policies.values()]
        match = re.fullmatch(r"/deviceManagement/configurationPolicies\('([^']+)'\)/(settings|assignments)", path)
        if match:
            return list(self.policies[match[1]][match[2]])
        raise AssertionError(f"unerwartet: GET {path}")


class RegistryTests(unittest.TestCase):
    def test_parses_entries_with_comments_and_quotes(self):
        extensions = intune.parse_registry(REGISTRY)

        self.assertEqual([e.name for e in extensions], ["sentinel-link", "demo"])
        self.assertEqual(extensions[0].comment, "Startseite: Kacheln # und Suche")
        self.assertEqual(extensions[0].owner, "ian.winter@blackforestsentinel.de")
        self.assertEqual(extensions[1].id, OTHER_ID)
        self.assertEqual(intune.desired_entries(extensions), sorted([LINK, OTHER]))

    @unittest.skipUnless(yaml, "PyYAML nicht installiert")
    def test_reads_like_yaml(self):
        samples = [REGISTRY, "extensions: []\n", "extensions:\n", "# nur Kommentar\nextensions:  # leer\n",
                   f"extensions:\n- name: abc\n  id: {LINK_ID}\n  updateUrl: '{LINK_URL}'\n  comment: \"It's x\"\n",
                   f"extensions:\n    - name: abc\n      id: {LINK_ID}\n      updateUrl: {LINK_URL}\n      owner: 'a ''b'''\n"]
        for sample in samples:
            with self.subTest(sample=sample):
                expected = (yaml.safe_load(sample) or {}).get("extensions") or []
                parsed = [{k: v for k, v in (("name", e.name), ("id", e.id), ("updateUrl", e.update_url),
                                             ("owner", e.owner), ("comment", e.comment)) if v}
                          for e in intune.parse_registry(sample)]
                self.assertEqual(parsed, expected)

    def test_empty_forms(self):
        for text in ("extensions: []\n", "extensions:\n", "extensions: [ ]  # noch keine\n\n# Ende\n"):
            with self.subTest(text=text):
                self.assertEqual(intune.parse_registry(text), [])

    def assertProblem(self, text, *fragments):
        with self.assertRaises(intune.RegistryError) as raised:
            intune.parse_registry(text, "extensions.yaml")
        message = "\n".join(raised.exception.problems)
        for fragment in fragments:
            self.assertIn(fragment, message)
        return raised.exception.problems

    def entry(self, **fields):
        values = {"name": "abc", "id": LINK_ID, "updateUrl": LINK_URL, **fields}
        lines = [f"{k}: {v}" for k, v in values.items() if v is not None]
        return "extensions:\n  - " + "\n    ".join(lines) + "\n"

    def test_rejects_format_problems(self):
        cases = {
            "": "„extensions:“ fehlt",
            "# nur Kommentar\n": "„extensions:“ fehlt",
            "erweiterungen:\n": "erwartet „extensions:“",
            "extensions: [abc]\n": "„[]“ für keinen",
            "extensions: []\n  - name: abc\n": "nach „extensions: []“",
            "extensions:\n\t- name: abc\n": "Tabulator",
            "extensions:\n  -name: abc\n": "genau ein Leerzeichen",
            f"extensions:\n  - name: abc\n      id: {LINK_ID}\n": "4 Leerzeichen",
            f"extensions:\n  - name: abc\n    id: {LINK_ID}\n - name: x\n": "einheitlich mit 2 Leerzeichen",
            "extensions:\n  - name: abc\nother: 1\n": "oberster Ebene",
            "extensions:\n  - name: abc\n    updateURL: x\n": "unbekannter Schlüssel „updateURL“",
            "extensions:\n  - name: abc\n    name: def\n": "doppelt",
            "extensions:\n  - name:abc\n": "Leerzeichen",
            "extensions:\n  - name: 'abc\n": "nicht geschlossen",
            "extensions:\n  - name: 'abc' x\n": "schließenden Anführungszeichen",
            'extensions:\n  - name: "a\\tb"\n': "Backslash",
            "extensions:\n  - name: [abc]\n": "Sonderzeichen",
            "extensions:\n  - name: a: b\n": "enthält „: “",
            "extensions:\n  - name: yes\n": "nicht als Text",
            "extensions:\n  - name: abc\n    comment: 2026-10-17\n": "nicht als Text",
            "extensions:\n  - name: abc\n    comment: |\n      x\n": "Sonderzeichen",
            "extensions:\n  - name: abc\n    just text\n": "unerwartete Zeile",
            "extensions:\n  - name: abc\n": "es fehlt id, updateUrl",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text):
                self.assertProblem(text, fragment)

    def test_reports_line_numbers_and_all_problems(self):
        problems = self.assertProblem("extensions:\n  - name: abc\n    foo: 1\n    bar: 2\n",
                                      "extensions.yaml, Zeile 3", "extensions.yaml, Zeile 4")
        self.assertGreaterEqual(len(problems), 3)

    def test_validates_values(self):
        cases = [
            (self.entry(name="Sentinel_Link"), "name „Sentinel_Link“"),
            (self.entry(name="ab"), "3 bis 40 Zeichen"),
            (self.entry(id="flpkgokniclbiocmpjpmficmfcdghfgz"), "32 Kleinbuchstaben von a bis p"),
            (self.entry(id=LINK_ID[:-1]), "id „"),
            (self.entry(updateUrl=LINK_URL.replace("https", "http")), "https://"),
            (self.entry(updateUrl="https://example.org/updates.xml"), f"auf {intune.UPDATES_PATH} enden"),
            (self.entry(updateUrl=LINK_URL + "?v=1"), "?… noch #…"),
            (self.entry(updateUrl="https://user:pw@example.org/api/extension/updates.xml"), "Anmeldedaten"),
            (self.entry(updateUrl="'https://example.org/a;b/api/extension/updates.xml'"), "„;“"),
        ]
        for text, fragment in cases:
            with self.subTest(text=text):
                self.assertProblem(text, fragment)

    def test_rejects_duplicates(self):
        text = (f"extensions:\n  - name: abc\n    id: {LINK_ID}\n    updateUrl: {LINK_URL}\n"
                f"  - name: abc\n    id: {LINK_ID}\n    updateUrl: {OTHER_URL}\n")

        self.assertProblem(text, "name „abc“ steht schon im Eintrag ab Zeile 2", f"id „{LINK_ID}“ steht schon")

    def test_tenant_template_is_valid(self):
        template = Path(__file__).resolve().parents[2] / "tenant" / "extensions.yaml"
        text = template.read_text(encoding="utf-8")

        self.assertEqual(intune.parse_registry(text), [])
        # Das Beispiel im Kopf, ohne Kommentarzeichen, ist ebenfalls gültig.
        lines = text.splitlines()
        start = lines.index("#   extensions:")
        end = lines.index("#", start)
        example = "\n".join(line[4:] for line in lines[start:end])
        self.assertEqual([e.name for e in intune.parse_registry(example)], ["kundenportal"])

    def test_read_registry_reports_missing_file(self):
        with self.assertRaises(intune.IntuneError):
            intune.read_registry(str(Path(tempfile.gettempdir(), "gibt-es-nicht.yaml")))


class PublishedTests(unittest.TestCase):
    extension = intune.Extension("sentinel-link", LINK_ID, LINK_URL)

    def test_manifest_with_matching_id_passes(self):
        self.assertIsNone(intune.manifest_problem(manifest(), LINK_ID))

    def test_real_manifest_from_sentinel_link(self):
        xml = (b'<?xml version="1.0" encoding="utf-8"?>\n<gupdate protocol="2.0" xmlns="http://www.google.com/update2/response">\n'
               b'  <app appid="flpkgokniclbiocmpjpmficmfcdghfgd">\n    <updatecheck codebase="https://func-sentinel-link-prod-f08084'
               b'.azurewebsites.net/api/extension/packages/sentinel-link-1.1.0.2185.crx" version="1.1.0.2185" hash_sha256="fb67" />\n'
               b'  </app>\n</gupdate>')

        self.assertIsNone(intune.manifest_problem(xml, LINK_ID))

    def test_manifest_problems(self):
        cases = [
            (manifest(appid=OTHER_ID), f"nennt nicht die ID {LINK_ID} (app/@appid: {OTHER_ID})"),
            (manifest(ns=False), "kein Update-Manifest"),
            (manifest(codebase=""), "kein Paket"),
            (manifest(codebase="http://example.org/a.crx"), "Edge braucht https"),
            (b"<html>", "kein gültiges XML"),
        ]
        for xml, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, intune.manifest_problem(xml, LINK_ID))

    def test_check_published_says_publish_first(self):
        def missing(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with quiet():
            problems = intune.check_published([self.extension], missing, delay=0)

        self.assertEqual(len(problems), 1)
        self.assertIn("antwortet mit 404", problems[0])
        self.assertIn("Erst veröffentlichen", problems[0])

    def test_check_published_retries_transient_errors(self):
        answers = [OSError("timeout"), urllib.error.HTTPError(LINK_URL, 503, "x", {}, None), manifest()]
        calls = []

        def flaky(url):
            calls.append(url)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with quiet():
            self.assertEqual(intune.check_published([self.extension], flaky, delay=0), [])
        self.assertEqual(len(calls), 3)

    def test_check_command(self):
        with tempfile.TemporaryDirectory() as work:
            registry = Path(work, "extensions.yaml")
            registry.write_text(REGISTRY, encoding="utf-8")
            served = {LINK_URL: manifest(LINK_ID), OTHER_URL: manifest(LINK_ID)}

            with quiet(), self.assertRaises(intune.IntuneError) as raised:
                intune.main(["check", "--registry", str(registry)], get=served.__getitem__)
            self.assertIn(f"demo ({OTHER_ID})", str(raised.exception))

            served[OTHER_URL] = manifest(OTHER_ID)
            with quiet() as out:
                self.assertEqual(intune.main(["check", "--registry", str(registry)], get=served.__getitem__), 0)
            self.assertIn("Alle Erweiterungen sind veröffentlicht", out.getvalue())

    def test_check_command_warns_on_empty_registry(self):
        with tempfile.TemporaryDirectory() as work:
            registry = Path(work, "extensions.yaml")
            registry.write_text("extensions: []\n", encoding="utf-8")

            with quiet() as out:
                self.assertEqual(intune.main(["check", "--registry", str(registry)], get=None), 0)
            self.assertIn("keine Erweiterung", out.getvalue())


class PlanTests(unittest.TestCase):
    def state(self, entries=None, assignments=None, policy=True, adopted_from=None, settings=None):
        if not policy:
            return intune.State()
        return intune.State(
            policy={"id": "p", "name": adopted_from or NAME},
            adopted_from=adopted_from,
            settings=settings if settings is not None else settings_for(entries or []),
            assignments=assignments if assignments is not None else [{"target": {"@odata.type": intune.ALL_USERS}}],
        )

    def test_setting_ids_come_from_definition(self):
        self.assertEqual(intune.setting_ids(DEFINITION), IDS)

    def test_creates_missing_policy(self):
        plan = intune.make_plan(self.state(policy=False), IDS, [LINK, OTHER])

        self.assertTrue(plan.create and plan.assign)
        self.assertEqual(len(plan.steps), 2)
        self.assertIn(f"+ {OTHER}", plan.steps[0])

    def test_nothing_to_do_when_policy_matches(self):
        plan = intune.make_plan(self.state([LINK, OTHER]), IDS, [LINK, OTHER])

        self.assertEqual(plan.steps, [])

    def test_lists_added_and_removed_entries(self):
        plan = intune.make_plan(self.state([LINK]), IDS, [OTHER])

        self.assertTrue(plan.replace_settings)
        self.assertEqual((plan.added, plan.removed), ([OTHER], [LINK]))
        self.assertIn(f"- {LINK} (Edge deinstalliert sie)", plan.steps[0])

    def test_order_and_duplicates_count_as_change(self):
        plan = intune.make_plan(self.state([OTHER, LINK, LINK]), IDS, sorted([LINK, OTHER]))

        self.assertIn("Reihenfolge", plan.steps[0])
        self.assertEqual(plan.removed, [])

    def test_disabled_setting_and_other_settings(self):
        settings = settings_for([LINK], enabled=False) + [{"settingInstance": {"settingDefinitionId": "anderes"}}]

        plan = intune.make_plan(self.state(settings=settings), IDS, [LINK])

        self.assertIn("bisher nicht aktiviert", plan.steps[0])
        self.assertIn("1 weitere Einstellungen entfernen", plan.steps[0])

    def test_replaces_assignments(self):
        assignments = [{"target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget", "groupId": "g"}}]

        plan = intune.make_plan(self.state([LINK], assignments=assignments), IDS, [LINK])

        self.assertTrue(plan.assign)
        self.assertEqual(plan.steps, ["Zuweisung ersetzen: groupAssignmentTarget -> alle Benutzer"])

    def test_adoption_renames(self):
        plan = intune.make_plan(self.state([LINK], adopted_from=OLD), IDS, [LINK, OTHER])

        self.assertTrue(plan.rename and plan.replace_settings)
        self.assertIn(f"„{OLD}“ übernehmen", plan.steps[0])
        self.assertEqual(plan.added, [OTHER])

    def test_empty_registry_stops_without_switch(self):
        for state in (self.state([LINK]), self.state(policy=False)):
            with self.subTest(state=state), self.assertRaises(intune.IntuneError) as raised:
                intune.make_plan(state, IDS, [])
            self.assertIn("deinstallieren", str(raised.exception))

    def test_empty_registry_with_switch_deletes_policy(self):
        plan = intune.make_plan(self.state([LINK, OTHER]), IDS, [], allow_empty=True)

        self.assertTrue(plan.delete)
        self.assertEqual(plan.removed, [LINK, OTHER])
        self.assertEqual(intune.make_plan(self.state(policy=False), IDS, [], allow_empty=True).steps, [])

    def test_plan_id_follows_steps(self):
        first = intune.make_plan(self.state([LINK]), IDS, [LINK, OTHER])
        same = intune.make_plan(self.state([LINK]), IDS, [LINK, OTHER])
        other = intune.make_plan(self.state([OTHER]), IDS, [LINK, OTHER])

        self.assertEqual(first.id, same.id)
        self.assertNotEqual(first.id, other.id)
        self.assertRegex(first.id, "^[0-9a-f]{16}$")

    def test_desired_policy_shape(self):
        body = intune.desired_policy(IDS, [LINK, OTHER], NAME, "Pipeline seed-intune")
        instance = body["settings"][0]["settingInstance"]

        self.assertEqual((body["name"], body["platforms"], body["technologies"]), (NAME, "windows10", "mdm"))
        self.assertIn("Pipeline seed-intune", body["description"])
        self.assertEqual(instance["choiceSettingValue"]["value"], IDS[1])
        child = instance["choiceSettingValue"]["children"][0]
        self.assertEqual(child["settingDefinitionId"], IDS[2])
        self.assertEqual([v["value"] for v in child["simpleSettingCollectionValue"]], [LINK, OTHER])
        json.dumps(body)


class StateTests(unittest.TestCase):
    def test_finds_own_policy(self):
        graph = FakeGraph()
        own = graph.add(NAME, [LINK])
        graph.add(OLD, [LINK])

        state = intune.read_state(graph, NAME, [OLD])

        self.assertEqual((state.policy["id"], state.adopted_from), (own, None))
        self.assertEqual([p["name"] for p in state.others], [OLD])

    def test_adopts_exactly_one_old_policy(self):
        graph = FakeGraph()
        old = graph.add(OLD, [LINK])
        graph.add("Fremd", [])

        state = intune.read_state(graph, NAME, [OLD, "Gibt es nicht"])

        self.assertEqual((state.policy["id"], state.adopted_from), (old, OLD))
        self.assertEqual(intune.entries_in(state.settings, IDS), ([LINK], 0))

    def test_refuses_several_candidates(self):
        graph = FakeGraph()
        graph.add(OLD, [LINK])
        graph.add("Alt 2", [OTHER])

        with self.assertRaises(intune.IntuneError) as raised:
            intune.read_state(graph, NAME, [OLD, "Alt 2"])
        self.assertIn("mehrere", str(raised.exception))

    def test_refuses_duplicate_names_and_wrong_platform(self):
        graph = FakeGraph()
        graph.add(NAME, [LINK])
        graph.add(NAME, [LINK])
        with self.assertRaises(intune.IntuneError):
            intune.read_state(graph, NAME, [])

        graph = FakeGraph()
        graph.add(OLD, [LINK], platforms="macOS")
        with self.assertRaises(intune.IntuneError) as raised:
            intune.read_state(graph, NAME, [OLD])
        self.assertIn("windows10/mdm", str(raised.exception))

    def test_conflicts(self):
        graph = FakeGraph()
        graph.add("Zugewiesen", [OTHER])
        graph.add("Nicht zugewiesen", [OTHER], assignments=[])
        graph.add("Nur Ausschluss", [OTHER], assignments=[{"target": {"@odata.type": intune.EXCLUSION, "groupId": "g"}}])
        graph.add("Andere Einstellung", settings=[{"settingInstance": {"settingDefinitionId": "x"}}])
        graph.add("Mac", [OTHER], platforms="macOS")
        # Chrome hat eine eigene Forcelist und stört Edge nicht.
        graph.add("Chrome", settings=[{"settingInstance": {"settingDefinitionId":
                  "device_vendor_msft_policy_config_chromeintunev1~policy~googlechrome~extensions_extensioninstallforcelist"}}])
        others = intune.read_state(graph, NAME, []).others

        errors, warnings = intune.find_conflicts(graph, others, IDS[0], [])

        self.assertEqual(len(errors), 1)
        self.assertIn("„Zugewiesen“", errors[0])
        self.assertEqual(len(warnings), 2)

    def test_conflict_names_adopt_hint(self):
        graph = FakeGraph()
        graph.add(NAME, [LINK])
        graph.add(OLD, [LINK])
        state = intune.read_state(graph, NAME, [OLD])

        errors, _ = intune.find_conflicts(graph, state.others, IDS[0], [OLD])

        self.assertIn("adoptPolicies", errors[0])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.registry = Path(self.work.name, "extensions.yaml")
        self.registry.write_text(REGISTRY, encoding="utf-8")
        self.graph = FakeGraph()

    def tearDown(self):
        self.work.cleanup()

    def run_command(self, *args, graph=None):
        argv = [args[0], "--registry", str(self.registry), *args[1:]]
        with quiet() as out:
            code = intune.main(argv, graph_factory=lambda: graph or self.graph)
        return code, out.getvalue()

    def apply(self, *args):
        command, options = intune.parse_args(["apply", "--registry", str(self.registry), *args])
        with quiet() as out:
            code = intune.run_apply(options, self.graph, delay=0)
        return code, out.getvalue()

    def plan_id(self, *args):
        _, out = self.run_command("plan", *args)
        return re.search(r"Plan ([0-9a-f]{16})", out)[1]

    def test_plan_reads_only(self):
        self.graph.add(OLD, [LINK])

        code, out = self.run_command("plan", "--adopt", OLD)

        self.assertEqual(code, 0)
        self.assertIn("übernehmen", out)
        self.assertEqual(self.graph.writes(), [])

    def test_plan_fails_on_assigned_conflict(self):
        self.graph.add(NAME, sorted([LINK, OTHER]))
        self.graph.add("Testrichtlinie", [LINK])

        with self.assertRaises(intune.IntuneError) as raised:
            self.run_command("plan")
        self.assertIn("„Testrichtlinie“", str(raised.exception))

    def test_apply_creates_and_assigns(self):
        code, out = self.apply()

        self.assertEqual(code, 0)
        (policy,) = self.graph.policies.values()
        self.assertEqual(policy["name"], NAME)
        self.assertEqual(intune.entries_in(policy["settings"], IDS), (sorted([LINK, OTHER]), 0))
        self.assertEqual(policy["assignments"], [{"target": {"@odata.type": intune.ALL_USERS}}])
        self.assertIn("Richtlinie entspricht dem Soll", out)

    def test_apply_adopts_without_delete(self):
        old = self.graph.add(OLD, [LINK], assignments=[{"target": {"@odata.type": "#microsoft.graph.groupAssignmentTarget"}}])

        self.apply("--adopt-json", json.dumps([OLD]))

        self.assertEqual(list(self.graph.policies), [old])
        self.assertEqual(self.graph.policies[old]["name"], NAME)
        self.assertEqual(intune.entries_in(self.graph.policies[old]["settings"], IDS), (sorted([LINK, OTHER]), 0))
        self.assertNotIn("DELETE", [m for m, _ in self.graph.writes()])
        self.assertEqual([m for m, _ in self.graph.writes()], ["PUT", "POST"])

    def test_apply_without_changes_writes_nothing(self):
        self.graph.add(NAME, sorted([LINK, OTHER]))

        code, out = self.apply()

        self.assertEqual(code, 0)
        self.assertIn("Keine Änderungen", out)
        self.assertEqual(self.graph.writes(), [])

    def test_apply_checks_approved_plan(self):
        self.graph.add(NAME, [LINK])
        approved = self.plan_id()
        self.registry.write_text(REGISTRY.replace("  - name: demo", "  - name: demo2"), encoding="utf-8")
        self.assertEqual(self.plan_id(), approved, "Der Name zählt nicht, nur die Einträge")
        self.graph.policies["p1"]["assignments"] = []

        with self.assertRaises(intune.IntuneError) as raised:
            self.apply("--expect-plan", approved)
        self.assertIn("seit der Freigabe geändert", str(raised.exception))
        self.assertEqual(self.graph.writes(), [])

        self.apply("--expect-plan", self.plan_id())
        self.assertEqual(self.plan_id(), intune.Plan().id)

    def test_apply_fails_when_result_differs(self):
        class Stubborn(FakeGraph):
            def request(self, method, path, body=None, missing_ok=False):
                if method == "PUT":
                    self.calls.append((method, path))
                    return {}
                return super().request(method, path, body, missing_ok)

        self.graph = Stubborn()
        self.graph.add(NAME, [LINK])

        with self.assertRaises(intune.IntuneError) as raised:
            self.apply()
        self.assertIn("Nach dem Anwenden fehlt noch", str(raised.exception))

    def test_empty_registry(self):
        self.registry.write_text("extensions: []\n", encoding="utf-8")
        policy = self.graph.add(NAME, [LINK])
        self.graph.add(OLD, [LINK])

        with self.assertRaises(intune.IntuneError):
            self.apply("--adopt", OLD)
        self.assertEqual(self.graph.writes(), [])

        self.apply("--adopt", OLD, "--allow-empty")
        self.assertNotIn(policy, self.graph.policies)
        self.assertEqual([p["name"] for p in self.graph.policies.values()], [OLD], "Alte Richtlinien bleiben")

    def test_arguments(self):
        cases = [
            ["--adopt-json", "{}"],
            ["--adopt-json", "kein json"],
            ["--policy-name", " "],
            ["--adopt", NAME],
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(intune.IntuneError):
                intune.parse_args(["plan", "--registry", "x", *args])
        _, options = intune.parse_args(["plan", "--registry", "x", "--adopt", "A", "--adopt-json", '["B", " "]'])
        self.assertEqual(options.adopt, ["A", "B"])


if __name__ == "__main__":
    unittest.main()
