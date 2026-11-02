import contextlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch

from flint import client
from test_flint import Fixture
import test_flint


class ClaudeOnboardingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config_dir = self.manager.home / "clients/claude/config"
        self.source = Path.home() / ".claude.json"
        self.source.parent.mkdir(parents=True)
        self.destination = self.config_dir / ".claude.json"
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def seed(self):
        value = dict(hasCompletedOnboarding=True, lastOnboardingVersion="2.1.1",
                     shiftEnterKeyBindingInstalled=False, optionAsMetaKeyInstalled=True,
                     oauthAccount={"token": "secret"}, apiKey="secret",
                     mcpServers={"untrusted": {"command": "do-not-run"}},
                     projects={"/trusted": {"hasTrustDialogAccepted": True,
                                            "allowedTools": ["Bash(*)"], "mcpServers": {"secret": "secret"}},
                               "/untrusted": {"hasTrustDialogAccepted": False},
                               "relative": {"hasTrustDialogAccepted": True}})
        self.source.write_text(json.dumps(value))
        return value

    def bootstrap(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            client.claude_onboarding(self.config_dir)
        self.assertNotIn("secret", stderr.getvalue())

    def test_only_allowlisted_state_copied_and_original_untouched(self):
        self.seed()
        original, stat = self.source.read_bytes(), self.source.stat()
        self.bootstrap()
        value = json.loads(self.destination.read_text())
        self.assertEqual(value, dict(hasCompletedOnboarding=True, lastOnboardingVersion="2.1.1",
                                     shiftEnterKeyBindingInstalled=False, optionAsMetaKeyInstalled=True,
                                     projects={"/trusted": {"hasTrustDialogAccepted": True}}))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.source.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config_dir.stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.config_dir.parent / "onboarding-import.json").is_file())

    def test_existing_decisions_and_settings_win_and_later_revocations_persist(self):
        self.seed()
        self.config_dir.mkdir(parents=True)
        current = {"hasCompletedOnboarding": False, "theme": "dark",
                   "projects": {"/trusted": {"hasTrustDialogAccepted": False, "custom": "keep"}}}
        self.destination.write_text(json.dumps(current))
        self.bootstrap()
        result = json.loads(self.destination.read_text())
        self.assertFalse(result["hasCompletedOnboarding"])
        self.assertEqual(result["projects"], current["projects"])
        self.assertEqual(result["theme"], "dark")
        self.destination.write_text(json.dumps(current))
        self.bootstrap()
        self.assertEqual(json.loads(self.destination.read_text()), current)

    def test_completed_import_is_not_repeated_and_respects_custom_source(self):
        self.seed()
        alternate = Path.home() / "custom"
        alternate.mkdir()
        (alternate / ".claude.json").write_text('{"hasCompletedOnboarding": false}')
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(alternate)}):
            self.bootstrap()
        self.assertEqual(json.loads(self.destination.read_text()), {"hasCompletedOnboarding": False})
        self.bootstrap()
        self.assertEqual(json.loads(self.destination.read_text()), {"hasCompletedOnboarding": False})

    def test_missing_source_does_not_invent_approvals(self):
        self.bootstrap()
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.config_dir.parent / "onboarding-import.json").exists())

    def test_malformed_config_or_symlink_does_not_get_overwritten(self):
        for raw in ("not json", "null", '{"hasCompletedOnboarding":"true"}',
                    '{"projects":[]}', '{"hasCompletedOnboarding":true,"hasCompletedOnboarding":false}'):
            self.source.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(SystemExit):
                self.bootstrap()
            self.assertFalse(self.destination.exists())
        self.seed()
        self.destination.symlink_to(self.source)
        original = self.source.read_bytes()
        with self.assertRaisesRegex(SystemExit, "심볼릭"):
            self.bootstrap()
        self.assertEqual(self.source.read_bytes(), original)

    def test_launcher_uses_bootstrapped_isolated_config(self):
        self.seed()
        _, _, env = test_flint.IntegrationTests.invoke_client(self, "claude")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.config_dir))
        self.assertEqual(json.loads(self.destination.read_text())["hasCompletedOnboarding"], True)

    def test_write_failure_is_reported_without_marking_import_complete(self):
        self.seed()
        with patch("flint.client.private_write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SystemExit, "disk full"):
                self.bootstrap()
        self.assertFalse((self.config_dir.parent / "onboarding-import.json").exists())
