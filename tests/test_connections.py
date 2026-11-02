import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flint.client import provider_config
from flint.connections import (ConfigError, Connections, GROUPS, JsonConfig, PROVIDER,
                               config_path, read_snapshot, write_checked)
from flint.core import DEFAULTS


class JsonConfigTests(unittest.TestCase):
    def test_only_provider_changes_with_comments_and_trailing_commas(self):
        original = '''{
  // keep my default
  "model": "other/model",
  "provider": {
    "other": {"options": {"baseURL": "https://example.test//x/*y*/"},},
  },
  "permission": {"bash": "ask"}, /* keep this */
}
'''
        document = JsonConfig(original, comments=True)
        updated = document.edit_provider("provider", {"models": {}})
        for text in ('// keep my default', '"model": "other/model"',
                     '"other": {"options": {"baseURL": "https://example.test//x/*y*/"},}',
                     '"permission": {"bash": "ask"}, /* keep this */'):
            self.assertIn(text, updated)
        parsed = JsonConfig(updated, comments=True).value
        self.assertEqual(parsed["provider"].pop(PROVIDER), {"models": {}})
        self.assertEqual(parsed, document.value)

    def test_add_update_remove_all_positions(self):
        for comments in (False, True):
            for group in GROUPS.values():
                for original in ({}, {group: {}}, {group: {"other": {"models": []}}},
                                 {"before": [1, {"nested": '},\\"'}], group: {}, "after": True}):
                    with self.subTest(original=original, comments=comments):
                        document = JsonConfig(json.dumps(original), comments)
                        text = document.edit_provider(group, {"models": ["flint"]})
                        updated = JsonConfig(text, comments).edit_provider(group, {"models": ["new"]})
                        parsed = JsonConfig(updated, comments).value
                        self.assertEqual(parsed[group][PROVIDER], {"models": ["new"]})
                        removed = JsonConfig(JsonConfig(updated, comments).edit_provider(group, None), comments).value
                        self.assertEqual(removed, {**original, group: original.get(group, {})})
            for providers in ({PROVIDER: {}, "a": {}}, {"a": {}, PROVIDER: {}, "b": {}},
                              {"a": {}, PROVIDER: {}}, {PROVIDER: {}}):
                for trailing in (False, True) if comments else (False,):
                    text = json.dumps({"provider": providers})
                    if trailing:
                        text = text[:-2] + ",}}"
                    result = JsonConfig(text, comments).edit_provider("provider", None)
                    self.assertEqual(JsonConfig(result, comments).value["provider"],
                                     {k: v for k, v in providers.items() if k != PROVIDER})

    def test_comment_after_last_value_and_escaped_provider_name(self):
        text = '{"provider":{"\\u0066lint-local":{} /* middle */, "a":{} // end\n}, "model":"a/m"}'
        removed = JsonConfig(text, True).edit_provider("provider", None)
        self.assertEqual(JsonConfig(removed, True).value, {"provider": {"a": {}}, "model": "a/m"})
        self.assertIn("/* middle */", removed)
        self.assertIn("// end", removed)
        added = JsonConfig(removed, True).edit_provider("provider", {})
        self.assertIn("// end", added)
        self.assertIn(PROVIDER, JsonConfig(added, True).value["provider"])

    def test_invalid_files_are_not_reformatted_or_repaired(self):
        for text in ("", "[]", "null", "{", '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}',
                     '{"bad": NaN}', '{"bad": 01}', '{"a":true false}', '{"a":"\n"}',
                     '{"a":1 /* unfinished }'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                JsonConfig(text, True)
        for text in ('{"a":1,}', '{/* comment */}', '{"a": [1,]}'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                JsonConfig(text)
        with self.assertRaises(ConfigError):
            JsonConfig('{"providers": null}').edit_provider("providers", {})


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "data with ' quotes"
        self.home.mkdir(mode=0o700)
        env = patch.dict(os.environ, {"HOME": str(self.root / "user"), "XDG_CONFIG_HOME": "",
                                    "PI_CODING_AGENT_DIR": ""})
        env.start()
        self.addCleanup(env.stop)
        self.settings = copy.deepcopy(DEFAULTS)
        self.connections = Connections(self.home, self.settings)

    def put(self, name, text):
        path = config_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def config(self, name):
        path = config_path(name)
        return JsonConfig(path.read_bytes().decode("utf-8"), name == "opencode").value

    def test_coexists_with_other_models_and_restores_exact_original(self):
        for name, group in GROUPS.items():
            with self.subTest(name=name):
                original = json.dumps({group: {"other": {"models": {"remote": {}}}},
                                       "model": "other/remote", "other": {"auth": "DO-NOT-EXPOSE"}},
                                      indent=4) + "\r\n"
                path = self.put(name, original)
                os.chmod(path, 0o640)
                self.connections.change(name, True)
                value = self.config(name)
                provider = value[group].pop(PROVIDER)
                self.assertEqual(value, json.loads(original))
                self.assertEqual(provider, provider_config(name, self.home, self.settings))
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                status = self.connections.status(name)
                self.assertTrue(status["enabled"] and status["can_remove"])
                self.assertNotIn("DO-NOT-EXPOSE", json.dumps(status))
                backup = Path(status["backup"])
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.connections.change(name, False)
                self.assertEqual(path.read_bytes(), original.encode())
                self.assertFalse(self.connections.status(name)["enabled"])
                self.assertTrue(backup.exists())

    def test_new_files_removed_on_disconnect_and_no_credentials_written(self):
        for name in GROUPS:
            self.connections.change(name, True)
            path = config_path(name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            provider = self.config(name)[GROUPS[name]][PROVIDER]
            key = provider.get("apiKey") if name == "pi" else provider["options"]["apiKey"]
            self.assertIn("rec/config/api-key", key)
            self.assertTrue(key.startswith("!/bin/cat ") if name == "pi" else key.startswith("{file:"))
            self.connections.change(name, False)
            self.assertFalse(path.exists())

    def test_rebrand_keeps_legacy_provider_owned_and_waits_for_explicit_update(self):
        self.assertEqual(PROVIDER, "flint-local")
        for name, group in GROUPS.items():
            with self.subTest(name=name):
                legacy = provider_config(name, self.home, self.settings)
                if name == "pi":
                    legacy["models"][0]["name"] = "Flint Local"
                else:
                    legacy["name"] = legacy["models"]["flint"]["name"] = "Flint Local"
                with patch("flint.connections.provider_config", return_value=legacy):
                    self.connections.change(name, True)
                path = config_path(name)
                before = path.read_bytes()
                status = self.connections.status(name)
                self.assertTrue(status["enabled"] and status["can_connect"] and status["needs_update"])
                self.assertFalse(status["error"])
                self.assertEqual(path.read_bytes(), before, "reading status never rewrites the old provider")
                self.connections.change(name, True)
                updated = self.config(name)[group]
                self.assertEqual(set(updated), {"flint-local"})
                model = updated[PROVIDER]["models"][0] if name == "pi" else updated[PROVIDER]["models"]["flint"]
                self.assertEqual(model["name"], "Flint Kit")
                self.assertFalse(self.connections.status(name)["needs_update"])
                self.connections.change(name, False)
                self.assertFalse(path.exists(), "the original empty config is still restored")

    def test_user_edits_to_unrelated_fields_survive_update_and_disconnect(self):
        for name, group in GROUPS.items():
            path = self.put(name, json.dumps({"model": "original", group: {"other": {}}}))
            self.connections.change(name, True)
            current = self.config(name)
            current["model"] = "new-default"
            current[group]["added-later"] = {"models": ["user"]}
            path.write_text(json.dumps(current))
            self.settings.update(context=131072, output_limit=4096, port=11541)
            self.assertTrue(self.connections.status(name)["needs_update"])
            self.connections.change(name, True)
            provider = self.config(name)[group][PROVIDER]
            self.assertEqual(provider, provider_config(name, self.home, self.settings))
            self.assertFalse(self.connections.status(name)["needs_update"])
            self.connections.change(name, False)
            del current[group][PROVIDER]
            self.assertEqual(self.config(name), current)
            self.settings.update(DEFAULTS)

    def test_jsonc_comment_and_unrelated_edit_survives_remove(self):
        directory = config_path("opencode").parent
        directory.mkdir(parents=True)
        path = directory / "opencode.jsonc"
        original = '{\n// keep comment\n"provider":{"other":{},}, "model":"other/one",\n}\n'
        path.write_text(original)
        self.connections.change("opencode", True)
        path.write_text(path.read_text().replace("other/one", "other/two"))
        self.connections.change("opencode", False)
        self.assertIn("// keep comment", path.read_text())
        self.assertIn('"model":"other/two"', path.read_text())
        self.assertEqual(self.config("opencode")["provider"], {"other": {}})

    def test_unowned_provider_and_modified_owned_provider_never_overwritten(self):
        for name, group in GROUPS.items():
            path = self.put(name, json.dumps({group: {PROVIDER: {"custom": True}}}))
            with self.assertRaisesRegex(ConfigError, "이미"):
                self.connections.change(name, True)
            self.assertIn('"custom": true', path.read_text())
            path.write_text("{}")
            self.connections.change(name, True)
            current = self.config(name)
            current[group][PROVIDER]["custom"] = True
            path.write_text(json.dumps(current))
            original = path.read_bytes()
            for enable in (True, False):
                with self.assertRaisesRegex(ConfigError, "외부에서"):
                    self.connections.change(name, enable)
                self.assertEqual(path.read_bytes(), original)
            status = self.connections.status(name)
            self.assertTrue(status["error"])
            self.assertFalse(status["can_connect"] or status["can_remove"])

    def test_paths_overrides_and_multiple_opencode_files(self):
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.root / "custom-pi"),
                                    "XDG_CONFIG_HOME": str(self.root / "custom-xdg")}):
            self.assertEqual(config_path("pi"), self.root / "custom-pi/models.json")
            self.assertEqual(config_path("opencode"), self.root / "custom-xdg/opencode/opencode.json")
        self.connections.change("pi", True)
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.root / "other-pi")}):
            with self.assertRaisesRegex(ConfigError, "경로가"):
                self.connections.change("pi", False)
        path = self.put("opencode", "{}")
        path.with_suffix(".jsonc").write_text("{}")
        with self.assertRaisesRegex(ConfigError, "여러 개"):
            self.connections.change("opencode", True)

    def test_visibility_restrictions_not_silently_changed(self):
        for config in ({"enabled_providers": ["other"]}, {"disabled_providers": [PROVIDER]}):
            self.put("opencode", json.dumps(config))
            with self.assertRaisesRegex(ConfigError, "차단"):
                self.connections.change("opencode", True)
            self.assertEqual(self.config("opencode"), config)
        self.put("opencode", json.dumps({"enabled_providers": ["other", PROVIDER]}))
        self.connections.change("opencode", True)
        self.assertTrue(self.connections.status("opencode")["enabled"])

    def test_symlink_and_parent_symlink_and_nonregular_rejected(self):
        victim = self.root / "victim"
        victim.write_text("{}")
        path = config_path("pi")
        path.parent.mkdir(parents=True)
        path.symlink_to(victim)
        with self.assertRaisesRegex(ConfigError, "심볼릭"):
            self.connections.change("pi", True)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaisesRegex(ConfigError, "일반 설정"):
            self.connections.change("pi", True)
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(self.root)
        with self.assertRaisesRegex(ConfigError, "심볼릭"):
            self.connections.change("pi", True)
        self.assertEqual(victim.read_text(), "{}")

    def test_concurrent_edit_is_preserved(self):
        path = self.put("pi", "{}")
        before = read_snapshot(path)
        path.write_text('{"user":true}')
        with self.assertRaisesRegex(ConfigError, "작업 중"):
            write_checked(path, before, b'{"new":true}')
        self.assertEqual(path.read_text(), '{"user":true}')

    def test_write_or_manifest_failure_restores_configuration(self):
        path = self.put("pi", '{"user":true}')
        for fail_at in (path, self.connections.manifest_path):
            real_write = write_checked
            def fail(target, before, content):
                if target == fail_at:
                    raise OSError("disk full")
                return real_write(target, before, content)
            with patch("flint.connections.write_checked", side_effect=fail):
                with self.assertRaises((ConfigError, OSError)):
                    self.connections.change("pi", True)
            self.assertEqual(path.read_text(), '{"user":true}')
            self.assertFalse(self.connections.status("pi")["enabled"])

    def test_failed_disconnect_manifest_commit_restores_connected_config(self):
        self.connections.change("pi", True)
        path = config_path("pi")
        before = path.read_bytes()
        def fail(target, snapshot, content):
            if target == self.connections.manifest_path:
                raise OSError("disk full")
            return write_checked(target, snapshot, content)
        with patch("flint.connections.write_checked", side_effect=fail):
            with self.assertRaisesRegex(ConfigError, "복원했습니다"):
                self.connections.change("pi", False)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(self.connections.status("pi")["enabled"])

    def test_backup_failure_prevents_edit_and_modified_backup_prevents_restore(self):
        path = self.put("pi", "{}")
        with patch("flint.connections.private_write", side_effect=OSError("backup full")):
            with self.assertRaises(OSError):
                self.connections.change("pi", True)
        self.assertEqual(path.read_text(), "{}")
        self.connections.change("pi", True)
        Path(self.connections.status("pi")["backup"]).write_text("{}")
        with self.assertRaisesRegex(ConfigError, "백업이 변경"):
            self.connections.change("pi", False)

    def test_rollback_failure_and_corrupt_manifest_are_not_success(self):
        path = self.put("pi", "{}")
        def fail(target, snapshot, content):
            if target == self.connections.manifest_path or content == b"{}":
                raise OSError("disk failure")
            return write_checked(target, snapshot, content)
        with patch("flint.connections.write_checked", side_effect=fail):
            with self.assertRaisesRegex(ConfigError, "복원도 실패"):
                self.connections.change("pi", True)
        self.assertIn(PROVIDER, path.read_text())
        self.assertIn("이미", self.connections.status("pi")["error"])
        self.connections.manifest_path.write_text('{"pi":true}')
        self.assertIn("기록 형식", self.connections.status("pi")["error"])

    def test_login_and_project_settings_are_not_touched(self):
        path = self.put("pi", "{}")
        auth = path.parent / "auth.json"
        auth.write_text('{"other":{"token":"private-fixture"}}')
        settings = path.parent / "settings.json"
        settings.write_text('{"defaultProvider":"other","defaultModel":"remote"}')
        self.connections.change("pi", True)
        self.connections.change("pi", False)
        self.assertEqual(auth.read_text(), '{"other":{"token":"private-fixture"}}')
        self.assertEqual(settings.read_text(), '{"defaultProvider":"other","defaultModel":"remote"}')

    def test_inputs_and_errors_are_explicit(self):
        for name, enable in (("claude", True), ("../escape", True), ("pi", 1), ("pi", "yes")):
            with self.assertRaises(ConfigError):
                self.connections.change(name, enable)
        self.put("pi", "invalid json")
        self.assertIn("JSON", self.connections.status("pi")["error"])
        self.assertFalse(self.connections.status("pi")["can_connect"])


if __name__ == "__main__":
    unittest.main()
