import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch, Mock

from flint.core import (CACHE_GB_CHOICES, CONTEXT_CHOICES, Cancelled, DEFAULTS, EngineStartError, EngineStartTimeout,
                        Manager, Problem, calibration_cache_gb, candidates, digest,
                        select_best, upper_median, validate)
from flint.web import make_server
from flint import client
from flint.hardware import (CHUNKS, HardwareDetector, defaults as hardware_defaults,
                            measurement_environment, requirements, tune_reason, tuning_plan,
                            worker_candidates, worker_options, worker_validity)

M4_PRO = dict(arm64=True, chip="Apple M4 Pro", gpu_cores=20,
              memory_bytes=48 * 2**30, macos="27.0", reasons={})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.hardware = copy.deepcopy(M4_PRO)
        self.detector = Mock()
        self.detector.snapshot.side_effect = lambda: copy.deepcopy(self.hardware)
        detector_patch = patch("flint.core.DETECTOR", self.detector)
        detector_patch.start()
        self.addCleanup(detector_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        home_patch = patch.dict(os.environ, {"HOME": str(Path(self.temp.name).resolve() / "user"),
                                           "PI_CODING_AGENT_DIR": "", "XDG_CONFIG_HOME": ""})
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.manager = Manager(Path(self.temp.name) / "data")

    def tearDown(self):
        self.temp.cleanup()


class PolicyTests(Fixture):
    def test_rebrand_preserves_default_data_directory(self):
        manager = Manager()
        self.assertEqual(manager.home, Path.home() / ".local/share/flint-local")
        self.assertFalse((Path.home() / ".local/share/flint-kit").exists())
        self.assertIn("Flint Kit", manager.logs[-1]["message"])

    def test_install_uses_official_download_without_other_apps(self):
        from flint.core import REC_SHA, REC_URL
        m = self.manager
        commands = []
        def run(args, **kwargs):
            commands.append([str(a) for a in args])
            if str(args[0]) == "/usr/bin/curl":
                Path(args[args.index("-o") + 1]).write_bytes(b"mock official rec")
        with patch("platform.system", return_value="Darwin"), \
                patch("platform.machine", return_value="arm64"), \
                patch("flint.core.digest", return_value=REC_SHA), patch.object(m, "run", side_effect=run):
            m.install()
        self.assertIn(REC_URL, commands[0])
        self.assertTrue((m.home / "bin/rec").is_file())
        self.assertNotIn("reference_models", m.status()["engine"])

    def test_exact_candidates(self):
        expected = [dict(workers=[d, 32, 32], chunk=c)
                    for d in (32, 36) for c in (1024, 2048, 4096)]
        self.assertEqual(candidates(self.hardware), expected)
        self.assertEqual(upper_median([3, 1, 4, 2]), 3)

    def test_strict_adoption(self):
        self.assertIsNone(select_best(100, [{"score": 103}]))
        self.assertEqual(select_best(100, [{"score": 103.000001}])["score"], 103.000001)
        self.assertIsNone(select_best(None, [{"score": 900}]))
        self.assertIsNone(select_best(100, [{"score": None}]))

    def test_settings_validation(self):
        self.assertEqual(DEFAULTS["context"], 65536)
        self.assertEqual(validate(copy.deepcopy(DEFAULTS)), DEFAULTS)
        for key, bad in [("port", 11539), ("context", -1), ("context", True),
                         ("temperature", float("nan")), ("workers", [800, 32, 32]),
                         ("disk_cache", "yes"), ("chunk", 1234), ("port", "11540"),
                         ("output_limit", 100000), ("context", None)]:
            with self.subTest(key=key, bad=bad), self.assertRaises(Problem):
                validate({**DEFAULTS, key: bad})
        with self.assertRaises(Problem):
            validate({**DEFAULTS, "shell": "whoami"})

    def test_confirmed_context_and_cache_choices(self):
        self.assertEqual(CONTEXT_CHOICES, (32768, 65536, 131072, 262144))
        self.assertEqual(CACHE_GB_CHOICES, (8, 16, 32, 64, 128, 192, 256, 384, 512))
        for value in CACHE_GB_CHOICES:
            with self.subTest(value=value):
                validate({**DEFAULTS, "cache_gb": value})
        with self.assertRaises(Problem):
            validate({**DEFAULTS, "cache_gb": 513})

    def test_cache_selection_exact_thresholds_and_fallback(self):
        self.assertEqual(calibration_cache_gb(0), 8)
        for index, value in enumerate(CACHE_GB_CHOICES):
            threshold = value * 5 * (2**30)
            with self.subTest(value=value):
                self.assertEqual(calibration_cache_gb(threshold - 1),
                                 CACHE_GB_CHOICES[max(0, index - 1)])
                self.assertEqual(calibration_cache_gb(threshold), value)
                self.assertEqual(calibration_cache_gb(threshold + 1), value)
        self.assertEqual(calibration_cache_gb(10000 * 2**30), 512)
        for bad in (-1, True, 1.5, None):
            with self.subTest(bad=bad), self.assertRaises(Problem):
                calibration_cache_gb(bad)

    def test_failed_save_does_not_change_live_configuration(self):
        with patch("flint.core.atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.manager.save({**DEFAULTS, "cache_gb": 16})
        self.assertEqual(self.manager.config, DEFAULTS)

    def test_secret_permissions_and_log_redaction(self):
        self.assertEqual(self.manager.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.manager.key_file.stat().st_mode & 0o777, 0o600)
        key = self.manager.key_file.read_text().strip()
        self.assertEqual(len(bytes.fromhex(key)), 32)
        self.manager.log("test " + key)
        self.assertNotIn(key, (self.manager.home / "activity.jsonl").read_text())

    def test_env_rejects_inherited_overrides(self):
        with patch.dict(os.environ, {"REC_HOST": "0.0.0.0", "PYTHONPATH": "/evil",
                                     "ANTHROPIC_API_KEY": "secret", "REC_EXP_WORKERS": "999"}):
            env = self.manager.env()
        self.assertEqual(env["REC_HOST"], "127.0.0.1")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["REC_EXP_WORKERS"], "36,32,32")

    def test_metrics_drops_sensitive_and_bad_records(self):
        path = self.manager.home / "metrics.jsonl"
        path.write_text('broken\n' + json.dumps(dict(status="complete", prompt="secret",
                        token_ids=[1, 2], decode_tokens_per_second=20)) + "\n")
        self.assertEqual(self.manager.metrics(), [dict(status="complete", decode_tokens_per_second=20)])

    def test_filler_shape(self):
        text = self.manager.filler(30, "12345678-ABCD")
        self.assertEqual(text, "Calibration 12345678-ABCD\n"
                         "Line 1 of calibration text 12345678 with a few ordinary words.\n"
                         "Line 2 of calibration text 12345678 with a few ordinary words.")


class IntegrationTests(Fixture):
    def test_alias_lifecycle_and_argument_forwarding(self):
        m = self.manager
        fake_home = Path(self.temp.name) / "user"
        with patch("flint.core.Path.home", return_value=fake_home):
            for name in ("pi", "claude", "codex", "opencode"):
                with self.subTest(name=name):
                    m.integration(name, True)
                    m.alias(name, True)
                    path = fake_home / ".local/bin" / (name + "-flint")
                    launcher = m.home / "clients" / name / "launch"
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(path.resolve(), launcher.resolve())
                    with patch.dict(os.environ, {"PATH": str(path.parent)}):
                        status = m.integration_status()[name]
                    self.assertTrue(status["alias"]["enabled"])
                    self.assertTrue(status["alias"]["on_path"])
                    with self.assertRaises(Problem):
                        m.integration(name, False)
                    with self.assertRaises(Problem):
                        m.alias(name, True)
                    # A harmless fixture proves the link preserves argument boundaries.
                    original = launcher.read_text()
                    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
                    result = subprocess.run([str(path), "two words", "--version"],
                                            capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout, "two words\n--version\n")
                    launcher.write_text(original)
                    m.alias(name, False)
                    self.assertFalse(os.path.lexists(path))
                    m.integration(name, False)
                    self.assertFalse(m.integration_status()[name]["alias"]["enabled"])

    def test_alias_requires_launcher_and_fixed_inputs(self):
        with patch("flint.core.Path.home", return_value=Path(self.temp.name) / "user"):
            for name, enable in [("pi", True), ("pi", False), ("../../escape", True),
                                 ("pi", "yes")]:
                with self.subTest(name=name, enable=enable), self.assertRaises(Problem):
                    self.manager.alias(name, enable)

    def test_alias_never_overwrites_existing_paths(self):
        m = self.manager
        fake_home = Path(self.temp.name) / "user"
        with patch("flint.core.Path.home", return_value=fake_home):
            m.integration("pi", True)
            path = fake_home / ".local/bin/pi-flint"
            path.parent.mkdir(parents=True)
            path.write_text("existing command")
            with self.assertRaises(Problem):
                m.alias("pi", True)
            self.assertEqual(path.read_text(), "existing command")
            path.unlink()
            path.symlink_to(path.parent / "missing")
            with self.assertRaises(Problem):
                m.alias("pi", True)
            self.assertEqual(os.readlink(path), str(path.parent / "missing"))

    def test_alias_rejects_modified_launcher(self):
        m = self.manager
        with patch("flint.core.Path.home", return_value=Path(self.temp.name) / "user"):
            m.integration("pi", True)
            launcher = m.home / "clients/pi/launch"
            launcher.write_text(launcher.read_text() + "# user change\n")
            with self.assertRaises(Problem):
                m.alias("pi", True)
            self.assertFalse(os.path.lexists(Path.home() / ".local/bin/pi-flint"))

    def test_alias_removal_protects_later_changes(self):
        m = self.manager
        with patch("flint.core.Path.home", return_value=Path(self.temp.name) / "user"):
            m.integration("pi", True)
            m.alias("pi", True)
            path = Path.home() / ".local/bin/pi-flint"
            original = os.readlink(path)
            path.unlink()
            path.symlink_to(path.parent / "replacement")
            with self.assertRaises(Problem):
                m.alias("pi", False)
            self.assertEqual(os.readlink(path), str(path.parent / "replacement"))
            path.unlink()
            path.write_text("new command")
            with self.assertRaises(Problem):
                m.alias("pi", False)
            self.assertEqual(path.read_text(), "new command")
            path.unlink()
            path.symlink_to(original)
            m.alias("pi", False)

    def test_enable_restore_newer_edits_protected(self):
        m = self.manager
        m.integration("claude", True)
        launcher = m.home / "clients/claude/launch"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertNotIn(m.key_file.read_text().strip(), launcher.read_text())
        original = launcher.read_text()
        launcher.write_text(original + "# user edit\n")
        with self.assertRaises(Problem):
            m.integration("claude", False)
        self.assertIn("# user edit", launcher.read_text())
        launcher.write_text(original)
        m.integration("claude", False)
        self.assertFalse(launcher.exists())

    def test_previous_wrapper_restored(self):
        path = self.manager.home / "clients/pi/launch"
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\necho previous\n")
        self.manager.integration("pi", True)
        self.manager.integration("pi", False)
        self.assertEqual(path.read_text(), "#!/bin/sh\necho previous\n")

    def test_unknown_clients(self):
        with self.assertRaises(Problem):
            self.manager.integration("../../etc", True)

    def invoke_client(self, name):
        response = Mock()
        response.__enter__ = Mock(return_value=Mock(status=200))
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch("sys.argv", ["client.py", name, str(self.manager.home)]), \
                patch("shutil.which", return_value="/mock/" + name), \
                patch("urllib.request.build_opener", return_value=opener), \
                patch("os.execvpe") as execute:
            client.main()
            return execute.call_args.args

    def test_claude_local_route_wins_over_cloud_environment(self):
        cloud = dict(CLAUDE_CODE_USE_BEDROCK="1", CLAUDE_CODE_USE_VERTEX="1",
                     CLAUDE_CODE_USE_FOUNDRY="1", ANTHROPIC_API_KEY="cloud-key",
                     AWS_SECRET_ACCESS_KEY="cloud-secret", GOOGLE_APPLICATION_CREDENTIALS="/cloud")
        with patch.dict(os.environ, cloud):
            executable, args, env = self.invoke_client("claude")
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", env)
        self.assertNotIn("CLAUDE_CODE_USE_FOUNDRY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:11540")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "flint")
        self.assertEqual(env["ANTHROPIC_CUSTOM_MODEL_OPTION_NAME"], "Flint Kit")
        self.assertTrue(env["CLAUDE_CONFIG_DIR"].startswith(str(self.manager.home)))
        self.assertNotIn("cloud-key", args)

    def test_codex_home_isolated(self):
        with patch.dict(os.environ, {"CODEX_HOME": "/original/codex"}):
            _, args, env = self.invoke_client("codex")
        self.assertEqual(env["CODEX_HOME"], str(self.manager.home / "clients/codex/config"))
        self.assertIn('model_provider="flint_local"', args)
        self.assertIn('model_providers.flint_local.name="Flint Kit"', args)
        self.assertIn('model_providers.flint_local.env_key="FLINT_LOCAL_KEY"', args)

    def test_pi_reads_keyfile_and_preserves_modified_config(self):
        self.invoke_client("pi")
        p = self.manager.home / "clients/pi/agent/models.json"
        content = p.read_text()
        self.assertNotIn(self.manager.key_file.read_text().strip(), content)
        self.assertTrue(json.loads(content)["providers"]["flint-local"]["apiKey"].startswith("!/bin/cat "))
        self.assertEqual(json.loads(content)["providers"]["flint-local"]["models"][0]["name"], "Flint Kit")
        p.write_text("{}")
        with self.assertRaises(SystemExit):
            self.invoke_client("pi")
        self.assertEqual(p.read_text(), "{}")

    def test_opencode_brand_preserves_provider_and_model_ids(self):
        _, _, env = self.invoke_client("opencode")
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], "flint-local/flint")
        self.assertEqual(config["provider"]["flint-local"]["name"], "Flint Kit")
        self.assertEqual(config["provider"]["flint-local"]["models"]["flint"]["name"], "Flint Kit")

    def test_pi_symlink_not_overwritten(self):
        directory = self.manager.home / "clients/pi/agent"
        directory.mkdir(parents=True)
        victim = self.manager.home / "other.json"
        victim.write_text("KEEP")
        (directory / "models.json").symlink_to(victim)
        with self.assertRaises(SystemExit):
            self.invoke_client("pi")
        self.assertEqual(victim.read_text(), "KEEP")

    def test_pi_app_owned_config_updates_after_settings_change(self):
        self.invoke_client("pi")
        self.manager.save({**DEFAULTS, "context": 131072, "port": 11541, "output_limit": 4096})
        self.invoke_client("pi")
        path = self.manager.home / "clients/pi/agent/models.json"
        provider = json.loads(path.read_text())["providers"]["flint-local"]
        self.assertEqual(provider["baseUrl"], "http://127.0.0.1:11541/v1")
        self.assertEqual(provider["models"][0]["contextWindow"], 131072)
        self.assertEqual(provider["models"][0]["maxTokens"], 4096)
        path.write_text("{}")
        with self.assertRaises(SystemExit):
            self.invoke_client("pi")
        self.assertEqual(path.read_text(), "{}")


class LifecycleTests(Fixture):
    def test_busy_operations_rejected(self):
        gate = threading.Event()
        self.manager.submit("wait", lambda: gate.wait(2))
        try:
            with self.assertRaises(Problem):
                self.manager.submit("second", lambda: None)
        finally:
            gate.set()
            with self.manager.lock:
                pass

    def test_owned_handles_only(self):
        fake = Mock()
        fake.pid = 123456
        fake.poll.return_value = None
        with patch("os.killpg") as kill:
            self.manager.proc = fake
            self.manager.stop()
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0], fake.pid)
        self.assertIsNone(self.manager.proc)

    def test_port_conflict_no_process_started(self):
        m = self.manager
        (m.home / "rec-tuned").mkdir()
        (m.home / "rec-tuned/manifest.json").write_text("{}")
        model = m.home / "rec/models/example"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            c = {**DEFAULTS, "port": sock.getsockname()[1]}
            with patch.object(m, "external_engine", return_value=False), patch("subprocess.Popen") as popen:
                with self.assertRaisesRegex(Problem, "포트") as error:
                    m.start(c)
                self.assertNotIsInstance(error.exception, EngineStartError)
                popen.assert_not_called()

    def test_startup_exit_is_error(self):
        m = self.manager
        (m.home / "rec-tuned").mkdir()
        (m.home / "rec-tuned/manifest.json").write_text("{}")
        model = m.home / "rec/models/example"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}")
        proc = Mock()
        proc.poll.return_value = 1
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with patch.object(m, "external_engine", return_value=False), \
                patch.object(m, "models_verified", return_value=True), \
                patch("flint.core.digest", return_value=__import__("flint.core", fromlist=["REC_SHA"]).REC_SHA), \
                patch("subprocess.Popen", return_value=proc):
            with self.assertRaisesRegex(EngineStartError, "종료"):
                m.start({**DEFAULTS, "port": port})
        self.assertFalse(m.running())

    def test_readiness_timeout_is_retryable_only_after_launch(self):
        m = self.manager
        (m.home / "rec-tuned").mkdir()
        (m.home / "rec-tuned/manifest.json").write_text("{}")
        model = m.home / "rec/models/example"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}")
        proc = Mock()
        proc.poll.return_value = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with patch.object(m, "external_engine", return_value=False), \
                patch.object(m, "models_verified", return_value=True), \
                patch("flint.core.digest", return_value=__import__("flint.core", fromlist=["REC_SHA"]).REC_SHA), \
                patch("subprocess.Popen", return_value=proc) as popen, \
                patch("flint.core.time.monotonic", side_effect=[0, 301]), \
                patch.object(m, "terminate") as terminate:
            with self.assertRaisesRegex(EngineStartTimeout, "시간 초과"):
                m.start({**DEFAULTS, "port": port})
            popen.assert_called_once()
            terminate.assert_called_once_with(proc)
        self.assertFalse(m.running())

    def test_cache_deletion_bounded(self):
        m = self.manager
        cache = m.home / "kv-cache"
        cache.mkdir()
        (cache / "data").write_text("KV")
        model = m.home / "models-marker"
        model.write_text("KEEP")
        m.clear_cache()
        self.assertFalse(cache.exists())
        self.assertTrue(model.exists())
        cache.symlink_to(m.home, target_is_directory=True)
        with self.assertRaises(Problem):
            m.clear_cache()


class TunerTests(Fixture):
    def test_settings_write_failure_restores_running_and_clears_admission(self):
        from flint.core import atomic
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.fake_engine()
                writes = 0
                def write(path, content, mode=0o600):
                    nonlocal writes
                    if path == self.manager.config_file:
                        writes += 1
                        if persistent or writes == 1:
                            raise OSError("injected settings write failure")
                    atomic(path, content, mode)
                with patch("flint.core.atomic", side_effect=write):
                    with self.assertRaisesRegex(Problem, "settings write failure"):
                        self.manager.tune()
                self.assertTrue(self.live)
                self.assertEqual(self.manager.config, DEFAULTS)
                self.assertIsNone(self.manager.benchmark_token)
                self.assertFalse(self.manager.cancel.is_set())
                self.assertEqual(self.manager.tuning["state"], "restore_error" if persistent else "error")
                self.assertFalse(self.manager.tuning["cache_selection"]["applied"])
                self.assertEqual(self.starts[-1], DEFAULTS)

    def test_result_write_failure_rolls_back_without_success_or_admission_leak(self):
        from flint.core import atomic
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.fake_engine()
                failed = False
                def write(path, content, mode=0o600):
                    nonlocal failed
                    if path == self.manager.home / "tuning.json":
                        final = json.loads(content)["state"] == "done"
                        if final or (failed and persistent):
                            failed = True
                            raise OSError("injected final result write failure")
                    atomic(path, content, mode)
                with patch("flint.core.atomic", side_effect=write):
                    with self.assertRaisesRegex(Problem, "result write failure"):
                        self.manager.tune()
                self.assertTrue(self.live)
                self.assertEqual(self.manager.config, DEFAULTS)
                self.assertEqual(self.manager.tuning["state"], "error")
                self.assertFalse(self.manager.tuning["adopted"])
                self.assertFalse(self.manager.tuning["cache_selection"]["applied"])
                self.assertIsNone(self.manager.benchmark_token)
                self.assertFalse(self.manager.cancel.is_set())
                self.assertEqual(self.starts[-1], DEFAULTS)
                persisted = json.loads((self.manager.home / "tuning.json").read_text())
                self.assertNotEqual(persisted["state"], "done")

    def test_all_result_writes_fail_restores_without_admission_leak(self):
        from flint.core import atomic
        self.fake_engine()
        def write(path, content, mode=0o600):
            if path == self.manager.home / "tuning.json":
                raise OSError("all result writes fail")
            atomic(path, content, mode)
        with patch("flint.core.atomic", side_effect=write):
            with self.assertRaisesRegex(Problem, "all result writes fail"):
                self.manager.tune()
        self.assertTrue(self.live)
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertIsNone(self.manager.benchmark_token)
        self.assertFalse(self.manager.cancel.is_set())
        self.assertEqual(self.manager.tuning["state"], "error")

    def test_settings_write_failure_does_not_start_previously_stopped_engine(self):
        from flint.core import atomic
        self.fake_engine(False)
        writes = 0
        def write(path, content, mode=0o600):
            nonlocal writes
            if path == self.manager.config_file:
                writes += 1
                if writes == 1:
                    raise OSError("settings write failure")
            atomic(path, content, mode)
        with patch("flint.core.atomic", side_effect=write):
            with self.assertRaisesRegex(Problem, "settings write failure"):
                self.manager.tune()
        self.assertFalse(self.live)
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertIsNone(self.manager.benchmark_token)
        self.assertEqual(self.manager.env()["FLINT_BENCHMARK_TOKEN"], "")
        self.assertEqual(self.manager.tuning["state"], "error")

    def fake_engine(self, running=True):
        disk = patch("flint.core.shutil.disk_usage", return_value=Mock(free=80 * 2**30))
        disk.start()
        self.addCleanup(disk.stop)
        m = self.manager
        self.live = running
        self.trial = None
        self.starts = []
        def start(c=None):
            self.live = True
            self.trial = c.copy()
            self.starts.append(c.copy())
        def stop():
            self.live = False
        def probe(prompt, limit, timeout, temperature=0.6, check=False):
            if check:
                return {"answer_sha256": "same"}
            score = 104 if self.trial["workers"][0] == 32 else 100
            return dict(decode_tokens_per_second=score, prompt_tokens=100, completion_tokens=256,
                        prefilled_tokens=8000, prefill_seconds=2)
        m.running = lambda: self.live
        m.start = start
        m.stop = stop
        m.probe = probe

    def test_real_policy_with_mock_measurements(self):
        self.fake_engine()
        self.manager.tune()
        self.assertEqual(len(self.manager.tuning["results"]), 7)
        self.assertTrue(self.manager.tuning["adopted"])
        self.assertEqual(self.manager.config["workers"], [36, 32, 32])
        self.assertEqual(self.manager.tuning["proposal"]["workers"], [32, 32, 32])
        self.assertTrue(self.manager.tuning["pending"])
        self.assertTrue(self.live)
        self.assertFalse(self.manager.benchmark_token)
        self.assertEqual(self.manager.tuning["proposal"]["cache_gb"], 16)
        self.assertFalse(self.manager.config["disk_cache"])
        self.assertFalse(self.manager.tuning["cache_selection"]["applied"])

    def test_context_and_cache_usage_preserved_with_resource_selection(self):
        original = {**DEFAULTS, "context": 131072, "disk_cache": True, "cache_gb": 64}
        self.manager.save(original)
        self.fake_engine()
        self.manager.tune()
        self.assertEqual(self.manager.config["context"], 131072)
        self.assertTrue(self.manager.config["disk_cache"])
        self.assertEqual(self.manager.config, original)
        self.assertEqual(self.manager.tuning["proposal"]["cache_gb"], 16)
        self.assertTrue(all(c["context"] == 131072 for c in self.starts))
        stored = json.loads((self.manager.home / "tuning.json").read_text())
        self.assertEqual(stored["context"], 131072)
        self.assertEqual(stored["settings_before"], original)
        self.assertEqual(stored["settings_after"], self.manager.tuning["proposal"])
        self.assertEqual(stored["cache_selection"]["free_bytes"], 80 * 2**30)
        self.assertEqual(stored["cache_selection"]["budget_gib"], 16)
        self.assertEqual(Manager(self.manager.home).config, self.manager.config)

    def test_cache_applies_without_performance_improvement_or_engine_start(self):
        self.fake_engine(False)
        probe = self.manager.probe
        def equal_probe(*args, **kwargs):
            row = probe(*args, **kwargs)
            if "decode_tokens_per_second" in row:
                row["decode_tokens_per_second"] = 100
            return row
        self.manager.probe = equal_probe
        self.manager.tune()
        self.assertFalse(self.manager.tuning["adopted"])
        self.assertEqual(self.manager.config["workers"], DEFAULTS["workers"])
        self.assertEqual(self.manager.config["cache_gb"], DEFAULTS["cache_gb"])
        self.assertEqual(self.manager.tuning["proposal"]["cache_gb"], 16)
        self.assertFalse(self.manager.tuning["cache_selection"]["applied"])
        self.assertFalse(self.live)

    def test_resource_selection_failure_rolls_back(self):
        self.fake_engine()
        with patch("flint.core.shutil.disk_usage", side_effect=OSError("disk query failed")):
            with self.assertRaisesRegex(OSError, "disk query failed"):
                self.manager.tune()
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertTrue(self.live)
        self.assertFalse(self.manager.benchmark_token)
        self.assertEqual(self.manager.tuning["state"], "error")

    def test_cancel_after_resource_selection_does_not_apply(self):
        self.fake_engine()
        def disk_usage(path):
            self.manager.cancel.set()
            return Mock(free=80 * 2**30)
        with patch("flint.core.shutil.disk_usage", side_effect=disk_usage):
            with self.assertRaises(Cancelled):
                self.manager.tune()
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertTrue(self.live)
        self.assertFalse(self.manager.tuning["cache_selection"]["applied"])

    def test_cache_minimum_fallback_is_recorded(self):
        self.fake_engine(False)
        with patch("flint.core.shutil.disk_usage", return_value=Mock(free=10 * 2**30)):
            self.manager.tune()
        selection = self.manager.tuning["cache_selection"]
        self.assertEqual(selection["selected_gb"], 8)
        self.assertTrue(selection["minimum_fallback"])
        self.assertFalse(self.manager.config["disk_cache"])

    def test_largest_cache_choice_persists_and_reaches_engine_environment(self):
        self.fake_engine(False)
        with patch("flint.core.shutil.disk_usage", return_value=Mock(free=3000 * 2**30)):
            self.manager.tune()
            self.manager.apply_tuning({"tuning_id": self.manager.tuning["tuning_id"]})
        self.assertEqual(self.manager.config["cache_gb"], 512)
        self.assertEqual(self.manager.env()["REC_DISK_CACHE_GB"], "512")
        self.assertEqual(Manager(self.manager.home).config["cache_gb"], 512)

    def test_failure_restores_config_and_running(self):
        self.fake_engine()
        self.manager.probe = Mock(side_effect=Problem("bad response"))
        with self.assertRaises(Problem):
            self.manager.tune()
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertTrue(self.live)
        self.assertFalse(self.manager.tuning["adopted"])

    def test_current_only_does_not_adopt(self):
        original = {**DEFAULTS, "context": 32768, "cache_gb": 64, "disk_cache": True}
        self.manager.save(original)
        self.fake_engine()
        with patch("flint.core.shutil.disk_usage", side_effect=AssertionError("baseline must not select cache")):
            self.manager.tune(current_only=True)
        self.assertEqual(len(self.manager.tuning["results"]), 1)
        self.assertEqual(len(self.manager.tuning["results"][0]["prefill_rounds"]), 3)
        self.assertEqual(self.manager.config, original)
        self.assertFalse(self.manager.tuning["adopted"])
        self.assertNotIn("cache_selection", self.manager.tuning)
        self.assertEqual(self.manager.tuning["context"], 32768)
        self.assertEqual(self.manager.tuning["measurement_context"], 65536)
        self.assertEqual(self.starts[0]["context"], 65536)
        self.assertEqual(self.starts[-1]["context"], 32768)

    def test_chunk_uses_prefill_not_decode(self):
        self.fake_engine()
        previous_probe = self.manager.probe
        def probe(*args, **kwargs):
            row = previous_probe(*args, **kwargs)
            if "prefill_seconds" in row:
                row["prefill_seconds"] = 1 if self.trial["chunk"] == 4096 else 2
            return row
        self.manager.probe = probe
        self.manager.tune()
        self.assertEqual(self.manager.tuning["proposal"]["chunk"], 4096)
        self.assertEqual(self.manager.tuning["proposal"]["workers"], [32, 32, 32])
        self.assertEqual(self.manager.config, DEFAULTS)

    def test_apply_failure_rolls_back_original(self):
        self.fake_engine()
        start = self.manager.start
        def apply(c):
            if self.manager.tuning.get("pending") and c["workers"] == [32, 32, 32]:
                raise Problem("candidate failed to restart")
            start(c)
        self.manager.start = apply
        self.manager.tune()
        with self.assertRaisesRegex(Problem, "원본으로 롤백"):
            self.manager.apply_tuning({"tuning_id": self.manager.tuning["tuning_id"]})
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertTrue(self.manager.tuning["pending"])
        self.assertTrue(self.live)
        self.assertFalse(self.manager.tuning["cache_selection"]["applied"])
        self.assertEqual(self.manager.tuning["settings_after"], self.manager.tuning["proposal"])

    def test_cancel_restores_previous_state(self):
        self.fake_engine(False)
        self.manager.probe = Mock(side_effect=Cancelled("cancel"))
        with self.assertRaises(Cancelled):
            self.manager.tune()
        self.assertEqual(self.manager.config, DEFAULTS)
        self.assertFalse(self.live)
        self.assertEqual(self.manager.tuning["state"], "cancelled")

    def test_restoration_error_surfaced(self):
        self.fake_engine()
        self.manager.probe = Mock(side_effect=Cancelled("cancel"))
        original = self.manager.start
        count = 0
        def start(c):
            nonlocal count
            count += 1
            if count > 1:
                raise Problem("restart failed")
            original(c)
        self.manager.start = start
        with self.assertRaisesRegex(Problem, "복원 실패"):
            self.manager.tune()
        self.assertEqual(self.manager.tuning["state"], "restore_error")

    def test_probe_rejects_malformed(self):
        self.manager.request = Mock(return_value={"choices": [], "usage": {}})
        with self.assertRaisesRegex(Problem, "응답"):
            self.manager.probe("x", 96, 1, check=True)

    def test_probe_correlates_single_numeric_metric(self):
        m = self.manager
        def request(*args, **kwargs):
            (m.home / "metrics.jsonl").write_text(json.dumps(dict(
                status="complete", prompt_tokens=10, decode_tokens_per_second=123.4)) + "\n")
            return dict(choices=[{"message": {"role": "assistant", "content": "answer"}}],
                        usage=dict(prompt_tokens=10, completion_tokens=128))
        m.request = request
        row = m.probe("x", 256, 1)
        self.assertEqual(row["decode_tokens_per_second"], 123.4)
        self.assertNotIn("content", row)

    def test_greedy_reasoning_only_response_is_hashed(self):
        self.manager.request = Mock(return_value=dict(
            choices=[{"message": {"role": "assistant", "reasoning": "valid but truncated reasoning"}}],
            usage=dict(prompt_tokens=14, completion_tokens=96)))
        row = self.manager.probe("x", 96, 1, check=True)
        self.assertEqual(len(row["answer_sha256"]), 64)

    def test_one_token_prefill_can_have_no_visible_content(self):
        m = self.manager
        def request(*args, **kwargs):
            (m.home / "metrics.jsonl").write_text(json.dumps(dict(
                status="complete", prompt_tokens=10, prefilled_tokens=10, prefill_seconds=0.1)) + "\n")
            return dict(choices=[{"message": {"role": "assistant"}}],
                        usage=dict(prompt_tokens=10, completion_tokens=1))
        m.request = request
        self.assertEqual(m.probe("x", 1, 1)["prefilled_tokens"], 10)


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.server = make_server(self.manager, self.port)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.port}"
        status, headers, body = self.call("GET", "/api/session")
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = json.loads(body)["csrf"]

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, auth=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Content-Type": "application/json"}
        if auth:
            h.update(Cookie=self.cookie, **{"X-Flint-CSRF": self.csrf, "Origin": self.origin})
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        res = conn.getresponse()
        result = res.status, dict(res.getheaders()), res.read()
        conn.close()
        return result

    def test_state_requires_session_and_csrf(self):
        self.assertEqual(self.call("GET", "/api/state")[0], 401)
        self.assertEqual(self.call("GET", "/api/state", auth=True)[0], 200)
        self.assertEqual(self.call("GET", "/api/state", headers={"Cookie": self.cookie})[0], 401)
        data = self.call("GET", "/api/state", auth=True)[2]
        self.assertNotIn(self.manager.key_file.read_text().strip().encode(), data)
        calibration = json.loads(data)["calibration"]
        self.assertEqual(calibration["context_choices"], list(CONTEXT_CHOICES))
        self.assertEqual(calibration["cache_gb_choices"], list(CACHE_GB_CHOICES))

    def test_history_requires_auth_and_only_accepts_known_uuids(self):
        import uuid
        identifier = str(uuid.uuid4())
        report = dict(tuning_id=identifier, state="done", context=65536, results=[])
        self.manager.archive_tuning(report)
        route = "/api/tune/history/" + identifier
        self.assertEqual(self.call("GET", route)[0], 401)
        self.assertEqual(self.call("GET", route, headers={"Origin": "https://evil.example"}, auth=True)[0], 403)
        response = self.call("GET", route, auth=True)
        self.assertEqual(response[0], 200)
        self.assertEqual(json.loads(response[2]), report)
        for path in ("../../settings.json", identifier + "?file=settings.json", str(uuid.uuid4())):
            self.assertEqual(self.call("GET", "/api/tune/history/" + path, auth=True)[0], 400)
        (self.manager.home / "tuning-history" / (identifier + ".json")).unlink()
        self.assertEqual(self.call("GET", route, auth=True)[0], 500)

    def test_chart_interval_query_is_read_only_validated_and_authenticated(self):
        snapshot = dict(memory={"state": "stopped"}, request_series=None)
        with patch.object(self.manager.monitor, "snapshot", return_value=snapshot) as monitor:
            for query, seconds in (("", 300), ("?chart_seconds=5", 5),
                                   ("?chart_seconds=60", 60), ("?chart_seconds=300", 300)):
                route = "/api/state" + query
                self.assertEqual(self.call("GET", route)[0], 401)
                self.assertEqual(self.call("GET", route, headers={"Origin": "https://evil.example"}, auth=True)[0], 403)
                response = self.call("GET", route, auth=True)
                self.assertEqual(response[0], 200)
                self.assertTrue(json.loads(response[2])["capabilities"]["chart_intervals"])
                self.assertEqual(json.loads(response[2])["capabilities"]["chart_interval_seconds"], [5, 60, 300])
                monitor.assert_called_with(None, seconds)
            monitor.reset_mock()
            for query in ("chart_seconds=0", "chart_seconds=-60", "chart_seconds=900", "chart_seconds=3600",
                          "chart_seconds=5.0", "chart_seconds=60.0", "chart_seconds=", "chart_seconds",
                          "chart_seconds=NaN", "chart_seconds=060", "x=1",
                          "chart_seconds=5&chart_seconds=60", "chart_seconds=60&x=1"):
                response = self.call("GET", "/api/state?" + query, auth=True)
                self.assertEqual(response[0], 400, query)
                self.assertIn("chart_seconds", json.loads(response[2])["error"])
            monitor.assert_not_called()
            self.assertEqual(self.manager.job["state"], "idle")

    def test_dns_rebinding_and_cross_origin(self):
        for h in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                  {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Upgrade": "websocket"}):
            with self.subTest(headers=h):
                self.assertEqual(self.call("GET", "/api/session", headers=h)[0], 403)
                self.assertEqual(self.call("POST", "/api/engine/start", {}, headers=h, auth=True)[0], 403)

    def test_mutation_requires_origin_csrf_json(self):
        self.assertEqual(self.call("POST", "/api/engine/stop", {})[0], 403)
        self.assertEqual(self.call("POST", "/api/engine/stop", {},
                                  headers={"Origin": ""}, auth=True)[0], 403)
        self.assertEqual(self.call("POST", "/api/engine/stop", {},
                                  headers={"Content-Type": "text/plain"}, auth=True)[0], 415)
        self.assertEqual(self.call("POST", "/api/install", {"source": "evil"}, auth=True)[0], 400)

    def test_alias_route_uses_authenticated_fixed_action(self):
        body = {"name": "pi", "enable": True}
        self.assertEqual(self.call("POST", "/api/alias", body)[0], 403)
        with patch.object(self.manager, "alias") as alias, \
                patch.object(self.manager, "submit", side_effect=lambda action, fn: fn()):
            self.assertEqual(self.call("POST", "/api/alias", body, auth=True)[0], 202)
            alias.assert_called_once_with("pi", True)

    def test_connection_route_preserves_auth_and_rejects_arbitrary_paths(self):
        body = {"name": "pi", "enable": True}
        self.assertEqual(self.call("POST", "/api/connection", body)[0], 403)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": ""},
                        {"X-Flint-CSRF": "invalid"}, {"Host": "evil.example"}):
            self.assertEqual(self.call("POST", "/api/connection", body, headers=headers, auth=True)[0], 403)
        for invalid in ({**body, "path": "/tmp/elsewhere"}, {"name": "claude", "enable": True},
                        {"name": "pi", "enable": "yes"}, {"name": "pi"}, {"name": [], "enable": True}):
            self.assertEqual(self.call("POST", "/api/connection", invalid, auth=True)[0], 400)
        with patch.object(self.manager, "submit", side_effect=lambda action, fn: fn()):
            for name in ("pi", "opencode"):
                self.assertEqual(self.call("POST", "/api/connection", {"name": name, "enable": True}, auth=True)[0], 202)
                state = json.loads(self.call("GET", "/api/state", auth=True)[2])
                self.assertTrue(state["capabilities"]["client_connections"])
                self.assertTrue(state["integrations"][name]["connection"]["enabled"])
                self.assertFalse(state["integrations"][name]["enabled"])
                self.assertNotIn(self.manager.key_file.read_text().strip(), json.dumps(state))
                self.assertEqual(self.call("POST", "/api/connection", {"name": name, "enable": False}, auth=True)[0], 202)

    def test_tune_apply_route_guards_and_capability(self):
        body = {"tuning_id": "run"}
        self.assertEqual(self.call("POST", "/api/tune/apply", body)[0], 403)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": ""},
                        {"X-Flint-CSRF": "invalid"}, {"Host": "evil.example"}):
            self.assertEqual(self.call("POST", "/api/tune/apply", body, headers=headers, auth=True)[0], 403)
        with patch.object(self.manager, "apply_tuning") as apply:
            self.assertEqual(self.call("POST", "/api/tune/apply", body, auth=True)[0], 200)
            apply.assert_called_once_with(body)
        self.assertEqual(self.call("POST", "/api/tune/apply", body, auth=True)[0], 409)

    def test_tune_apply_authenticated_transaction(self):
        TunerTests.fake_engine(self)
        self.manager.tune()
        body = {"tuning_id": self.manager.tuning["tuning_id"]}
        self.assertEqual(self.call("POST", "/api/tune/apply", body, auth=True)[0], 200)
        self.assertTrue(self.manager.tuning["applied"])
        self.assertEqual(self.call("POST", "/api/tune/apply", body, auth=True)[0], 409)

    def test_cancel_during_restoration_is_rejected(self):
        self.manager.restoring = True
        self.assertEqual(self.call("POST", "/api/cancel", {}, auth=True)[0], 400)
        self.assertFalse(self.manager.cancel.is_set())

    def test_no_arbitrary_path_or_command_api(self):
        for path in ("/api/exec", "/api/files", "/../flint/core.py", "/.env"):
            self.assertEqual(self.call("GET", path, auth=True)[0], 404)
        self.assertEqual(self.call("POST", "/api/exec", {"cmd": "pwd"}, auth=True)[0], 404)
        self.assertEqual(self.call("POST", "/api/models/reuse", {}, auth=True)[0], 404)
        self.assertEqual(self.call("POST", "/api/install", {"source": "reference"}, auth=True)[0], 400)

    def test_headers_and_assets(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Flint Kit", body)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.call("OPTIONS", "/api/settings")[0], 403)

    def test_brand_and_favicon_are_local_and_served_with_correct_mime(self):
        status, headers, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Flint Kit — Flint 실행부터 튜닝까지".encode(), body)
        self.assertIn(b'rel="icon" type="image/svg+xml" sizes="any" href="/favicon.svg"', body)
        self.assertIn(b'class="mark" src="/favicon.svg"', body)
        self.assertNotIn(b"Flint Local", body)
        self.assertIn("FlintKit", headers["Server"])
        for route in ("/favicon.svg", "/favicon.svg?v=kit"):
            status, headers, svg = self.call("GET", route)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "image/svg+xml")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            root = ET.fromstring(svg)
            self.assertEqual(root.attrib["viewBox"], "0 0 64 64")
            self.assertTrue(all(node.tag.rsplit("}", 1)[-1] in {"svg", "rect", "path"} for node in root.iter()))
            self.assertNotIn(b"href", svg)
        self.assertEqual(self.call("GET", "/favicon.svg", headers={"Origin": "https://evil.example"})[0], 403)

    def test_navigation_query_and_bundled_assets(self):
        status, _, body = self.call("GET", "/?flint-workspace.step=tuning")
        self.assertEqual(status, 200)
        self.assertIn(b'src="/navigation.js"', body)
        self.assertIn(b'id="decode-chart"', body)
        self.assertIn(b'id="memory-meter"', body)
        self.assertIn(b'<option value="5">', body)
        self.assertNotIn(b'<option value="900">', body)
        self.assertNotIn(b'id="monitor-visual"', body)
        self.assertNotIn(b'id="overview-visual"', body)
        for path in ("/navigation.js", "/visuals.js", "/app.js?flint-workspace.step=setup"):
            status, headers, body = self.call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
            self.assertEqual(headers["Content-Security-Policy"].split(";")[1].strip(), "script-src 'self'")
            self.assertNotIn(b"new Function", body)
        for path in ("/frontend/package.json", "/static/visuals.js", "/navigation.js/",
                     "/visuals.js/../app.js"):
            self.assertEqual(self.call("GET", path, auth=True)[0], 404, path)
        self.assertNotEqual(self.call("GET", f"http://127.0.0.1:{self.port}/navigation.js", auth=True)[0], 200)


if __name__ == "__main__":
    unittest.main()
