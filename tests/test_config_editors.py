import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from core.configuration import dump, parse, validate_config
from config_editor_schema import Field, LAUNCH_FIELDS, update_fields, field_text
from config_editor_server import ConfigStore, EditConflict, apply_operations, make_server


EXAMPLE = """# Keep this heading
defaults:
  poll_interval: 2  # interval comment
queues:
- name: Demo
  times: ['06:00']
  tasks:
  - name: First  # first task comment
    type: launch
    exe: 'C:/Program Files/First.exe'
    args: ['--name', 'space, comma']
    extension: {preserve: true}  # extension comment
  - name: Second
    type: kill
    targets: [{names: [Second.exe]}]
"""
TASK = ["queues", 0, "tasks", 0]


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.yaml"
        self.path.write_text(EXAMPLE, encoding="utf-8")
        self.store = ConfigStore(self.path)

    def body(self, operations):
        return {"revision": self.store.revision, "operations": operations}

    def test_precise_nested_edit_preserves_comments_unknown_fields_and_backup(self):
        body = self.body([{"op": "set", "path": TASK + ["args"], "value": ["--new", "  spaces, commas  ", ""]}])
        self.store.process("save", body)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# interval comment", text)
        self.assertIn("# first task comment", text)
        self.assertIn("# extension comment", text)
        task = parse(text)["queues"][0]["tasks"][0]
        self.assertEqual(task["extension"], {"preserve": True})
        self.assertEqual(task["args"], ["--new", "  spaces, commas  ", ""])
        self.assertEqual(self.path.with_suffix(".yaml.bak").read_text(encoding="utf-8"), EXAMPLE)

    def test_list_edits_keep_order_comments_and_original_draft(self):
        data = parse(EXAMPLE)
        result = apply_operations(data, [
            {"op": "move", "path": ["queues", 0, "tasks"], "from": 0, "to": 1},
            {"op": "set", "path": ["queues", 0, "tasks", 1, "name"], "value": "Renamed"},
            {"op": "append", "path": ["queues", 0, "tasks", 0, "targets"], "value": {"names": ["Third.exe"]}},
        ])
        self.assertEqual([t["name"] for t in result["queues"][0]["tasks"]], ["Second", "Renamed"])
        self.assertIn("# first task comment", dump(result))
        self.assertEqual(data["queues"][0]["tasks"][0]["name"], "First")
        self.assertEqual(len(result["queues"][0]["tasks"][0]["targets"]), 2)

    def test_global_yaml_preserves_queues(self):
        result = apply_operations(parse(EXAMPLE), [{"op": "yaml", "path": [], "text": "defaults: {poll_interval: 3}\n# New heading\n"}])
        self.assertEqual(result["queues"][0]["name"], "Demo")
        self.assertEqual(result["defaults"]["poll_interval"], 3)
        validate_config(result)

    def test_invalid_config_and_yaml_do_not_write_or_make_backups(self):
        for op in (
            {"op": "set", "path": ["queues", 0, "times"], "value": ["25:00"]},
            {"op": "yaml", "path": TASK, "text": "[invalid"},
        ):
            with self.assertRaises(Exception):
                self.store.process("save", self.body([op]))
            self.assertEqual(self.path.read_text(encoding="utf-8"), EXAMPLE)
            self.assertFalse(self.path.with_suffix(".yaml.bak").exists())

    def test_external_changes_are_not_overwritten(self):
        body = self.body([{ "op": "set", "path": TASK + ["name"], "value": "Changed"}])
        changed = EXAMPLE + "# External change\n"
        self.path.write_text(changed, encoding="utf-8")
        with self.assertRaises(EditConflict):
            self.store.process("save", body)
        self.assertEqual(self.path.read_text(encoding="utf-8"), changed)

    def test_old_browser_revision_is_rejected_after_another_save(self):
        old = self.body([])
        self.store.process("save", self.body([{"op": "set", "path": TASK + ["name"], "value": "Changed"}]))
        with self.assertRaises(EditConflict):
            self.store.process("save", old)

    def test_preview_does_not_save_and_supports_new_nested_matchers(self):
        preview = self.store.process("preview", self.body([{"op": "append", "path": TASK + ["resolution_check", "matchers"], "value": {"names": ["Game.exe"]}}]))
        self.assertEqual(preview["data"]["queues"][0]["tasks"][0]["resolution_check"]["matchers"][0]["names"], ["Game.exe"])
        self.assertEqual(self.path.read_text(encoding="utf-8"), EXAMPLE)

    def test_invalid_move_and_negative_path_are_rejected(self):
        for op in ({"op": "move", "path": ["queues"], "from": -1, "to": 0},
                   {"op": "delete", "path": ["queues", -1]}):
            with self.assertRaises(ValueError):
                apply_operations(parse(EXAMPLE), [op])


class FieldPatchTests(unittest.TestCase):
    def test_untouched_defaults_are_not_materialized(self):
        source = parse("exe: Demo.exe\nextension: 123 # Keep me\n")
        initial = {f.key: source.get(f.key, f.default) if f.kind == "bool" else field_text(source.get(f.key, f.default), f.kind) for f in LAUNCH_FIELDS}
        result = update_fields(source, LAUNCH_FIELDS, initial, initial)
        self.assertEqual(dump(result), dump(source))
        self.assertNotIn("args", result)

    def test_arguments_keep_spaces_commas_backslashes_and_empty_argument(self):
        fields = [Field("args", "参数", "lines", [])]
        result = update_fields({}, fields, {"args": "C:\\Program Files\\Script.py\n space,comma \n"}, {"args": ""})
        self.assertEqual(result["args"], ["C:\\Program Files\\Script.py", " space,comma ", ""])

    def test_cleared_engine_args_and_optional_numbers_inherit(self):
        fields = [Field("ue", "UE 参数", "optional_lines", []), Field("timeout", "超时", "number")]
        result = update_fields({"ue": ["-windowed"], "timeout": 10}, fields,
                               {"ue": "", "timeout": ""}, {"ue": "-windowed", "timeout": "10"})
        self.assertEqual(result, {})

    def test_nonfinite_and_noninteger_values_report_field_name(self):
        for kind, text in (("number", "nan"), ("int", "1.5")):
            with self.assertRaisesRegex(ValueError, "运行时限"):
                update_fields({}, [Field("timeout", "运行时限", kind)], {"timeout": text}, {"timeout": ""})


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "config.yaml"
        cls.path.write_text(EXAMPLE, encoding="utf-8")
        cls.server = make_server(cls.path, 0, "test-only-token")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def test_loopback_api_requires_token(self):
        url = f"http://127.0.0.1:{self.server.server_port}/api/config"
        with self.assertRaises(HTTPError) as error:
            urlopen(url, timeout=3)
        self.assertEqual(error.exception.code, 403)
        request = Request(url, headers={"Authorization": "Bearer test-only-token"})
        with urlopen(request, timeout=3) as response:
            result = json.load(response)
        self.assertEqual(result["data"]["queues"][0]["name"], "Demo")
        self.assertIn("resolution_args", result["schemas"])


if __name__ == "__main__":
    unittest.main()
