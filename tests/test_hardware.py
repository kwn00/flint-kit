import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from flint.core import DEFAULTS, Manager, Problem, candidates, validate
from flint.hardware import (CHUNKS, HardwareDetector, defaults, measurement_environment,
                            macos_requirement, requirements, tune_reason, tuning_plan, worker_candidates,
                            worker_options, worker_validity)

M4 = dict(arm64=True, chip="Apple M4 Pro", gpu_cores=20,
          memory_bytes=48 * 2**30, macos="27.0", reasons={})


class DetectorTests(unittest.TestCase):
    def detector(self):
        self.outputs = {
            "hw.optional.arm64": "1\n", "machdep.cpu.brand_string": "Apple M4 Pro\n",
            "hw.memsize": "51539607552\n", "AGXAccelerator": '  "gpu-core-count" = 20\n',
        }
        def run(args, **kwargs):
            key = args[2]
            value = self.outputs[key]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(args, 0, stdout=value, stderr="")
        self.runner = Mock(side_effect=run)
        self.now = 0
        return HardwareDetector(runner=self.runner, system=lambda: "Darwin",
                                macos=lambda: ("27.0", (), ""), clock=lambda: self.now)

    def test_exact_sources_sanitized_environment_and_cached_success(self):
        detector = self.detector()
        self.assertEqual(detector.snapshot(), M4)
        self.assertEqual(detector.snapshot(), M4)
        self.now = 10000
        self.assertEqual(detector.snapshot(), M4)
        self.assertEqual(self.runner.call_count, 4)
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(commands, [
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
            ["/usr/sbin/ioreg", "-rc", "AGXAccelerator", "-d", "1"],
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
        ])
        for call in self.runner.call_args_list:
            self.assertFalse(call.kwargs["shell"])
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertEqual(set(call.kwargs["env"]), {"PATH", "LANG", "LC_ALL"})

    def test_missing_gpu_retried_only_with_delay_and_bounded_attempts(self):
        detector = self.detector()
        self.outputs["AGXAccelerator"] = ""
        for now, expected_calls in [(0, 4), (1, 4), (59, 4), (60, 5), (120, 6), (999, 6)]:
            self.now = now
            result = detector.snapshot()
            self.assertIsNone(result["gpu_cores"])
            self.assertIn("gpu_cores", result["reasons"])
            self.assertEqual(self.runner.call_count, expected_calls)
        self.assertEqual(result["chip"], "Apple M4 Pro")

    def test_missing_value_recovers_without_rereading_successful_fields(self):
        detector = self.detector()
        self.outputs["AGXAccelerator"] = ""
        detector.snapshot()
        self.now = 60
        self.outputs["AGXAccelerator"] = '"gpu-core-count" = 40'
        self.assertEqual(detector.snapshot()["gpu_cores"], 40)
        self.assertEqual(self.runner.call_count, 5)
        self.assertNotIn("gpu_cores", detector.snapshot()["reasons"])

    def test_missing_malformed_timeout_oserror_and_nonzero(self):
        scenarios = [
            ("hw.optional.arm64", "2", "arm64"),
            ("hw.optional.arm64", "true", "arm64"),
            ("machdep.cpu.brand_string", "", "chip"),
            ("machdep.cpu.brand_string", "Apple M4 Pro\nmalformed", "chip"),
            ("hw.memsize", "48 GB", "memory_bytes"),
            ("hw.memsize", "0", "memory_bytes"),
            ("AGXAccelerator", '"gpu-core-count" = -20', "gpu_cores"),
            ("AGXAccelerator", '"gpu-core-count" = 513', "gpu_cores"),
            ("AGXAccelerator", '"gpu-core-count" = 20\n"gpu-core-count" = 40', "gpu_cores"),
            ("AGXAccelerator", subprocess.TimeoutExpired("ioreg", 5), "gpu_cores"),
            ("hw.memsize", OSError("unavailable"), "memory_bytes"),
        ]
        for key, output, field in scenarios:
            with self.subTest(key=key, output=output):
                detector = self.detector()
                self.outputs[key] = output
                result = detector.snapshot()
                self.assertIsNone(result[field])
                self.assertTrue(result["reasons"][field])
        detector = self.detector()
        self.runner.side_effect = lambda args, **kw: subprocess.CompletedProcess(args, 1, stdout="20", stderr="secret")
        result = detector.snapshot()
        self.assertTrue(all(result[field] is None for field in ("arm64", "chip", "gpu_cores", "memory_bytes")))
        self.assertNotIn("secret", json.dumps(result))

    def test_non_mac_and_bad_version_are_unknown_not_guessed(self):
        runner = Mock()
        result = HardwareDetector(runner=runner, system=lambda: "Linux").snapshot()
        self.assertTrue(all(value is None for key, value in result.items() if key != "reasons"))
        runner.assert_not_called()
        detector = self.detector()
        detector.macos = lambda: ("invalid", (), "")
        self.assertIsNone(detector.snapshot()["macos"])

    def test_thread_safe_single_detection(self):
        detector = self.detector()
        results = []
        threads = [threading.Thread(target=lambda: results.append(detector.snapshot())) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [M4] * 6)
        self.assertEqual(self.runner.call_count, 4)


class HardwarePolicyTests(unittest.TestCase):
    def test_macos_version_boundary_and_unknown_are_gated_consistently(self):
        spec = importlib.util.spec_from_file_location(
            "flint_installer", Path(__file__).resolve().parents[1] / "vendor/rec-overlay/install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        for version, expected in (("14.9.9", False), ("15.0", True), ("15.0.1", True),
                                  ("27.0", True), ("", None), (None, None), ("15beta", None)):
            with self.subTest(version=version):
                hardware = {**M4, "macos": version}
                self.assertIs(macos_requirement(hardware)["passed"], expected)
                self.assertEqual(bool(tune_reason(hardware)), expected is not True)
                problems = installer.unmet("Darwin", "arm64", "Apple M4 Pro",
                                            48 * 2**30, 32 * 2**30, False, version)
                self.assertEqual(bool(problems), expected is not True)

    def test_unsupported_os_blocks_actions_before_any_engine_or_download(self):
        with tempfile.TemporaryDirectory() as directory:
            for version in ("14.7", "", None):
                with self.subTest(version=version):
                    m = Manager(Path(directory), Mock(snapshot=Mock(return_value={**M4, "macos": version})))
                    self.assertIsNot(m.status()["hardware"]["macos_check"]["passed"], True)
                    with patch("platform.system", return_value="Darwin"), \
                            patch("platform.machine", return_value="arm64"), \
                            patch.object(m, "run") as run, patch.object(m, "external_engine") as external, \
                            patch.object(m, "stop") as stop:
                        for action in (m.install, m.reuse_models, m.start, m.tune,
                                       lambda: m.model_command("pull"), lambda: m.model_command("doctor")):
                            with self.assertRaisesRegex(Problem, "macOS 15"):
                                action()
                        run.assert_not_called()
                        external.assert_not_called()
                        stop.assert_not_called()

    def test_chip_core_candidates_and_defaults(self):
        cases = [
            ("Apple M4 Pro", 20, [32, 36], [32], 2, [36, 32, 32], 2048),
            ("Apple M4 Max", 32, [32, 36, 40, 48, 56, 60], [32], 6, [36, 32, 32], 2048),
            ("Apple M4 Max", 40, [40, 48, 56, 64, 72, 76], [32, 64], 9, [76, 64, 64], 2048),
            ("Apple M5 Pro", 20, [32, 36], [32], 2, [36, 32, 32], 4096),
            ("Apple M5 Max", 40, [40, 48, 56, 64, 72, 76], [32, 64], 9, [76, 64, 64], 4096),
            ("Apple M4 Max", None, [], [], 0, [36, 32, 32], 2048),
            ("Apple M5 Max", None, [], [], 0, [76, 64, 64], 4096),
            ("Apple M3 Max", 40, [], [], 0, [36, 32, 32], 2048),
        ]
        for chip, cores, decode, mlp, count, default, chunk in cases:
            with self.subTest(chip=chip, cores=cores):
                hardware = {**M4, "chip": chip, "gpu_cores": cores}
                rows = worker_candidates(hardware)
                self.assertEqual(sorted({w[0] for w in rows}), decode)
                self.assertEqual(sorted({w[1] for w in rows}), mlp)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(m == h and m <= d for d, m, h in rows))
                self.assertEqual(defaults(hardware), dict(workers=default, chunk=chunk))
                self.assertEqual(len(candidates(hardware)), count * len(CHUNKS))

    def test_rounding_ties_away_and_cap_not_rounded_up(self):
        hardware = {**M4, "gpu_cores": 34}
        decode = sorted({w[0] for w in worker_candidates(hardware)})
        self.assertIn(36, decode)
        self.assertIn(64, decode)
        self.assertNotIn(68, decode)

    def test_tuning_requirements_fail_with_explicit_reason(self):
        for change in (dict(arm64=False), dict(arm64=None), dict(chip="Apple M3 Pro"),
                       dict(chip=None), dict(memory_bytes=47 * 2**30),
                       dict(memory_bytes=None), dict(gpu_cores=None)):
            with self.subTest(change=change):
                self.assertTrue(tune_reason({**M4, **change}))
        self.assertFalse(tune_reason(M4))
        checks = requirements(M4, 10 * 2**30, True)
        self.assertTrue(all(c["passed"] for c in checks))
        self.assertFalse(requirements(M4, 10 * 2**30, False)[-1]["passed"])
        self.assertTrue(requirements(M4, 32 * 2**30, False)[-1]["passed"])
        self.assertFalse(requirements(M4, 10 * 2**30 - 1, True)[-1]["passed"])
        self.assertFalse(tune_reason(M4))  # Disk is advisory, not a tuner gate.

    def test_validation_and_unverifiable_vs_invalid_workers(self):
        large = {**M4, "chip": "Apple M4 Max", "gpu_cores": 40}
        self.assertEqual(validate({**DEFAULTS, "workers": [76, 64, 64]}, large)["workers"], [76, 64, 64])
        with self.assertRaises(Problem):
            validate({**DEFAULTS, "workers": [76, 64, 64]}, M4)
        unknown = {**M4, "gpu_cores": None}
        self.assertEqual(worker_validity([76, 64, 64], unknown)["state"], "unverified")
        self.assertEqual(worker_options(unknown), [[36, 32, 32]])
        self.assertEqual(worker_validity([76, 64, 64], M4)["state"], "invalid")
        with self.assertRaises(Problem):
            validate({**DEFAULTS, "workers": [32, 32, 32]}, unknown)
        for workers in ([True, 32, 32], [36, 32], "36,32,32", [0, 0, 0]):
            with self.subTest(workers=workers), self.assertRaises(Problem):
                validate({**DEFAULTS, "workers": workers}, M4)

    def test_new_defaults_and_existing_settings_preserved_without_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            large = {**M4, "chip": "Apple M5 Max", "gpu_cores": 40}
            detector = Mock(snapshot=Mock(return_value=large))
            m = Manager(home, detector)
            self.assertEqual(m.config["workers"], [76, 64, 64])
            self.assertEqual(m.config["chunk"], 4096)
            self.assertFalse(m.config_file.exists())
            m.config_file.write_text(json.dumps(DEFAULTS))
            before = m.config_file.read_bytes()
            stat = m.config_file.stat()
            detector.snapshot.return_value = {**M4, "gpu_cores": None}
            m = Manager(home, detector)
            self.assertEqual(m.config, DEFAULTS)
            self.assertEqual(m.config_file.read_bytes(), before)
            self.assertEqual(m.config_file.stat().st_mtime_ns, stat.st_mtime_ns)

    def test_saved_other_mac_settings_boot_but_start_and_tune_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "settings.json").write_text(json.dumps({**DEFAULTS, "workers": [76, 64, 64]}))
            m = Manager(home, Mock(snapshot=Mock(return_value=M4)))
            self.assertEqual(m.status()["hardware"]["workers_status"]["state"], "invalid")
            with patch.object(m, "external_engine") as external, patch.object(m, "stop") as stop:
                with self.assertRaisesRegex(Problem, "허용 후보"):
                    m.start()
                with self.assertRaisesRegex(Problem, "허용 후보"):
                    m.tune()
                external.assert_not_called()
                stop.assert_not_called()
            self.assertEqual(m.config["workers"], [76, 64, 64])
            with self.assertRaises(Problem):
                m.save(m.config)

    def test_unknown_cores_do_not_block_start_or_discard_saved_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            unknown = {**M4, "gpu_cores": None, "reasons": {"gpu_cores": "temporary"}}
            home = Path(directory)
            saved = {**DEFAULTS, "workers": [32, 32, 32]}
            (home / "settings.json").write_text(json.dumps(saved))
            detector = Mock(snapshot=Mock(return_value=unknown))
            m = Manager(home, detector)
            self.assertEqual(m.status()["hardware"]["workers_status"]["state"], "unverified")
            with patch.object(m, "external_engine", return_value=True) as external:
                with self.assertRaisesRegex(Problem, "다른 추론"):
                    m.start()
                external.assert_called_once()  # Hardware uncertainty didn't block the existing start path.
            m.save({**saved, "context": 131072})
            with self.assertRaises(Problem):
                m.save({**saved, "workers": [40, 32, 32]})
            with patch.object(m, "stop") as stop:
                with self.assertRaisesRegex(Problem, "GPU 코어"):
                    m.tune()
                stop.assert_not_called()

    def test_state_only_meaningful_fields_and_environment_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            hardware = copy.deepcopy(M4)
            detector = Mock(snapshot=Mock(side_effect=lambda: copy.deepcopy(hardware)))
            m = Manager(Path(directory), detector)
            state = m.status()
            self.assertEqual(state["hardware"]["chip"], M4["chip"])
            self.assertEqual(state["hardware"]["memory_gib"], 48)
            self.assertNotIn("physicalcpu", json.dumps(state["hardware"]))
            self.assertNotIn("hw.model", json.dumps(state["hardware"]))
            self.assertNotIn("perflevel", json.dumps(state["hardware"]))
            self.assertEqual(state["tuning_environment_status"], "missing")
            m.tuning["environment"] = measurement_environment(hardware)
            self.assertEqual(m.status()["tuning_environment_status"], "same")
            hardware["macos"] = "27.1"
            self.assertEqual(m.status()["tuning_environment_status"], "different")
            hardware["gpu_cores"] = None
            self.assertEqual(m.status()["tuning_environment_status"], "unknown")

    def test_status_refreshes_disk_and_warns_when_default_workers_cannot_be_fully_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            unknown = {**M4, "gpu_cores": None, "reasons": {"gpu_cores": "temporary"}}
            m = Manager(Path(directory), Mock(snapshot=Mock(return_value=unknown)))
            with patch("flint.core.shutil.disk_usage", side_effect=[Mock(free=32 * 2**30), Mock(free=8 * 2**30)]):
                first, second = m.status(), m.status()
            self.assertTrue(first["hardware"]["checks"][-1]["passed"])
            self.assertFalse(second["hardware"]["checks"][-1]["passed"])
            self.assertNotEqual(first["hardware"]["free_bytes"], second["hardware"]["free_bytes"])
            self.assertEqual(first["hardware"]["workers_status"]["state"], "valid")
            self.assertTrue(first["hardware"]["workers_status"]["reason"])
            self.assertFalse(first["tuning_plan"]["allowed"])

    def test_runner_and_ui_plan_use_same_candidates_and_record_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            hardware = {**M4, "chip": "Apple M4 Max", "gpu_cores": 40}
            m = Manager(Path(directory), Mock(snapshot=Mock(return_value=hardware)))
            starts = []
            def start(config):
                starts.append(copy.deepcopy(config))
            m.start = start
            m.stop = Mock()
            m.running = Mock(return_value=False)
            m.probe = Mock(side_effect=lambda *a, **k: {"answer_sha256": "same"} if k.get("check") else
                           dict(decode_tokens_per_second=100, prefilled_tokens=8000, prefill_seconds=2))
            state = m.status()
            with patch("flint.core.shutil.disk_usage", return_value=Mock(free=80 * 2**30)):
                m.tune()
            actual = [r["candidate"]["workers"] for r in m.tuning["results"] if r["phase"] == "workers"]
            self.assertEqual(actual, state["tuning_plan"]["workers"])
            self.assertEqual(len(actual), 9)
            self.assertEqual([r["candidate"]["chunk"] for r in m.tuning["results"] if r["phase"] == "chunk"],
                             [2048, 1024, 4096])
            self.assertEqual(m.tuning["environment"], measurement_environment(hardware))
            self.assertEqual(m.tuning["worker_candidates"], actual)
            self.assertEqual(len(starts), len(actual) + 3)
            self.assertEqual(m.config["context"], DEFAULTS["context"])

    def test_tune_gates_before_stopping_for_all_missing_or_unsupported_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            for change in (dict(arm64=False), dict(chip="Apple M3 Pro"),
                           dict(memory_bytes=16 * 2**30), dict(memory_bytes=None),
                           dict(gpu_cores=None)):
                with self.subTest(change=change):
                    m = Manager(Path(directory), Mock(snapshot=Mock(return_value={**M4, **change})))
                    with patch.object(m, "stop") as stop, patch.object(m, "start") as start:
                        with self.assertRaisesRegex(Problem, "튜닝 불가"):
                            m.tune()
                        stop.assert_not_called()
                        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
