import copy
import json
from unittest.mock import Mock, patch

from flint.core import Cancelled, DEFAULTS, EngineStartError, EngineStartTimeout, Manager, Problem, atomic
from flint.hardware import hung_dominates, worker_options
import test_flint


class RecoveryTests(test_flint.Fixture):
    fake_engine = test_flint.TunerTests.fake_engine

    def failing_current(self, error=None):
        self.manager.save({**DEFAULTS, "workers": [32, 32, 32]})
        self.fake_engine(False)
        start = self.manager.start
        calls = []
        def fail(config):
            calls.append(copy.deepcopy(config))
            if config["workers"] == [32, 32, 32]:
                raise error or EngineStartError("current startup failed")
            start(config)
        self.manager.start = fail
        return calls

    def tune_without_delay(self, **kwargs):
        with patch.object(self.manager.cancel, "wait", return_value=False):
            self.manager.tune(**kwargs)

    def test_fallback_recovers_without_claiming_original_performance_or_auto_apply(self):
        calls = self.failing_current()
        m = self.manager
        before = copy.deepcopy(m.config)
        self.tune_without_delay()
        result = m.tuning
        self.assertEqual([c["workers"] for c in calls[:3]],
                         [[32, 32, 32], [32, 32, 32], [36, 32, 32]])
        self.assertEqual(result["fallback"]["state"], "used")
        self.assertTrue(result["recovered"])
        self.assertFalse(result["adopted"])
        self.assertTrue(result["pending"])
        self.assertFalse(result["applied"])
        self.assertEqual(result["proposal"]["workers"], [36, 32, 32])
        self.assertEqual(result["verification"]["state"], "passed")
        self.assertEqual(result["verification"]["baseline"], result["measurement_baseline"])
        self.assertIn("원래 설정 대비 속도 향상은 확인하지 못", result["verification"]["reason"])
        self.assertEqual(result["results"][1]["phase"], "fallback_baseline")
        self.assertEqual(result["results"][1]["answer_hashes"], ["same"] * 3)
        self.assertIsNone(result["results"][0]["score"])
        self.assertEqual(m.config, before)
        self.assertFalse(self.live)
        self.assertEqual(result["progress"]["total"], len(result["results"]))
        m.apply_tuning({"tuning_id": result["tuning_id"]})
        self.assertEqual(m.config["workers"], [36, 32, 32])
        self.assertTrue(self.live)

    def test_large_mac_accepts_rec_default_without_changing_normal_search_grid(self):
        self.hardware.update(chip="Apple M4 Max", gpu_cores=40)
        m = self.manager
        m.save({**DEFAULTS, "workers": [76, 64, 64]})
        self.fake_engine(False)
        start = m.start
        def fail(config):
            if config["workers"] != [36, 32, 32]:
                raise EngineStartError("startup exit")
            start(config)
        m.start = fail
        self.assertIn([36, 32, 32], worker_options(self.hardware))
        self.assertEqual(m.status()["tuning_plan"]["worker_count"], 9)
        self.tune_without_delay()
        self.assertEqual(m.tuning["proposal"]["workers"], [36, 32, 32])
        self.assertEqual(m.tuning["progress"]["total"], 15)
        self.assertEqual(len(m.tuning["results"]), 15)
        self.assertTrue(m.tuning["recovered"])
        m.apply_tuning({"tuning_id": m.tuning["tuning_id"]})
        self.assertEqual(m.config["workers"], [36, 32, 32])

    def test_current_only_preflight_and_measurement_failures_never_use_fallback(self):
        m = self.manager
        for error, current_only in ((EngineStartError("no startup"), True),
                                    (Problem("bad model integrity"), False),
                                    (OSError("spawn failed"), False), (Cancelled("cancel"), False)):
            with self.subTest(error=error, current_only=current_only):
                calls = self.failing_current(error)
                with self.assertRaises((Problem, OSError)):
                    self.tune_without_delay(current_only=current_only)
                self.assertEqual(m.tuning["fallback"]["state"], "not_used")
                self.assertTrue(all(c["workers"] == [32, 32, 32] for c in calls))
                self.assertFalse(m.tuning["pending"])
        self.fake_engine(False)
        m.probe = Mock(side_effect=EngineStartError("probe is not startup"))
        with self.assertRaisesRegex(Problem, "probe is not startup"):
            self.tune_without_delay()
        self.assertEqual(m.tuning["fallback"]["state"], "not_used")

    def test_failed_fallback_or_failed_final_recovery_has_no_proposal(self):
        m = self.manager
        for final_failure in (False, True):
            with self.subTest(final_failure=final_failure):
                self.failing_current()
                if not final_failure:
                    m.start = Mock(side_effect=EngineStartTimeout("not ready"))
                else:
                    probe = m.probe
                    def regress(*args, **kwargs):
                        row = probe(*args, **kwargs)
                        if not kwargs.get("check") and m.tuning["progress"]["phase"] == "verify_candidate":
                            row["decode_tokens_per_second"] = 90
                        return row
                    m.probe = regress
                with self.assertRaises(Problem):
                    self.tune_without_delay()
                self.assertFalse(m.tuning["pending"])
                self.assertFalse(m.tuning["recovered"])
                self.assertIsNone(m.tuning["proposal"])
                self.assertEqual(m.config["workers"], [32, 32, 32])
                self.assertFalse(self.live)
                self.assertIsNone(m.benchmark_token)

    def test_cancel_during_fallback_preserves_original(self):
        self.failing_current()
        m = self.manager
        start = m.start
        def cancel(config):
            if m.tuning["progress"]["phase"] == "fallback_baseline":
                raise Cancelled("cancel fallback")
            start(config)
        m.start = cancel
        with self.assertRaisesRegex(Cancelled, "cancel fallback"):
            self.tune_without_delay()
        self.assertEqual(m.tuning["fallback"]["state"], "cancelled")
        self.assertEqual(m.config["workers"], [32, 32, 32])
        self.assertFalse(self.live)

    def test_worker_pruning_is_componentwise_and_only_after_startup_timeout(self):
        self.hardware.update(chip="Apple M4 Max", gpu_cores=40)
        m = self.manager
        m.save({**DEFAULTS, "workers": [40, 32, 32]})
        for error in (EngineStartTimeout("hung"), EngineStartError("exited")):
            with self.subTest(error=error):
                self.fake_engine(False)
                start = m.start
                def fail(config):
                    if config["workers"] == [64, 64, 64]:
                        raise error
                    start(config)
                m.start = fail
                self.tune_without_delay()
                rows = [r for r in m.tuning["results"] if r["phase"] == "workers"]
                skipped = [r["candidate"]["workers"] for r in rows if r.get("skipped")]
                self.assertEqual(skipped, [[72, 64, 64], [76, 64, 64]]
                                 if isinstance(error, EngineStartTimeout) else [])
                self.assertTrue(any(r["candidate"]["workers"] == [76, 32, 32] and r["score"] for r in rows))
                for row in rows:
                    if row.get("skipped"):
                        self.assertEqual(row["start_attempts"], 0)
                        self.assertIn("시간 초과", row["skip_reason"])
                self.assertEqual(m.tuning["progress"]["completed"], m.tuning["progress"]["total"])
                self.assertEqual(m.tuning["progress"]["eta_seconds"], 0)

    def test_chunk_pruning_and_other_worker_axes_are_separate(self):
        self.fake_engine(False)
        m = self.manager
        start = m.start
        def fail(config):
            if m.tuning["progress"]["phase"] == "chunk" and config["chunk"] == 1024:
                raise EngineStartTimeout("hung chunk")
            start(config)
        m.start = fail
        self.tune_without_delay()
        rows = [r for r in m.tuning["results"] if r["phase"] == "chunk"]
        self.assertTrue(rows[2]["skipped"])
        self.assertEqual(rows[2]["candidate"]["chunk"], 4096)
        failed = dict(workers=[64, 64, 64], chunk=2048)
        self.assertFalse(hung_dominates(failed, dict(workers=[76, 32, 32], chunk=2048), "workers"))
        self.assertFalse(hung_dominates(failed, dict(workers=[76, 64, 64], chunk=1024), "workers"))
        self.assertFalse(hung_dominates(failed, dict(workers=[76, 64, 64], chunk=4096), "chunk"))

    def test_measurement_timeout_is_not_a_hung_startup(self):
        self.fake_engine(False)
        m = self.manager
        probe = m.probe
        def timeout(*args, **kwargs):
            if self.trial["workers"] == [32, 32, 32]:
                raise EngineStartTimeout("probe timeout")
            return probe(*args, **kwargs)
        m.probe = timeout
        self.tune_without_delay()
        self.assertFalse(any(r.get("skipped") for r in m.tuning["results"]))
        self.assertTrue(all(r["failure_kind"] is None for r in m.tuning["results"]))


class HistoryTests(test_flint.Fixture):
    fake_engine = test_flint.TunerTests.fake_engine

    def test_history_is_persistent_grouped_by_environment_and_configured_context(self):
        self.fake_engine(False)
        m = self.manager
        ids = []
        for context in (32768, 65536, 32768):
            m.save({**m.config, "context": context})
            m.tune(current_only=True)
            ids.append(m.tuning["tuning_id"])
        rows = [m.tuning_history[i] for i in ids]
        self.assertEqual(rows[0]["key"], rows[2]["key"])
        self.assertNotEqual(rows[0]["key"], rows[1]["key"])
        self.assertEqual(rows[0]["context"], 32768)
        self.assertEqual(m.historical_tuning(ids[0])["measurement_context"], 65536)
        self.hardware["macos"] = "28.0"
        m.tune(current_only=True)
        self.assertNotEqual(m.tuning_history[m.tuning["tuning_id"]]["key"], rows[0]["key"])
        restored = Manager(m.home, self.detector)
        self.assertEqual(len(restored.status()["tuning_history"]), 4)
        self.assertEqual(restored.config, m.config)
        for identifier in ids:
            self.assertEqual(restored.historical_tuning(identifier)["tuning_id"], identifier)
        self.assertEqual(restored.tuning, m.tuning)
        with self.assertRaises(Problem):
            restored.apply_tuning({"tuning_id": ids[0]})

    def test_previous_legacy_record_is_kept_before_new_measurement(self):
        m = self.manager
        m.tuning = dict(state="done", results=[], adopted=False)
        self.fake_engine(False)
        m.tune(current_only=True)
        self.assertEqual(len(m.tuning_history), 2)
        legacy = next(r for r in m.tuning_history.values() if r["context"] is None)
        self.assertEqual(m.historical_tuning(legacy["tuning_id"])["results"], [])

    def test_cancelled_failed_and_applied_records_are_archived(self):
        self.fake_engine(False)
        m = self.manager
        m.tune()
        identifier = m.tuning["tuning_id"]
        m.apply_tuning({"tuning_id": identifier})
        self.assertTrue(m.historical_tuning(identifier)["applied"])
        self.assertTrue(m.tuning_history[identifier]["applied"])
        for error in (Cancelled("cancel"), Problem("failure")):
            self.fake_engine(False)
            m.probe = Mock(side_effect=error)
            with self.assertRaises(Problem):
                m.tune()
            stored = m.historical_tuning(m.tuning["tuning_id"])
            self.assertIn(stored["state"], ("cancelled", "error"))
            self.assertIsNone(stored["proposal"])

    def test_history_write_failure_is_explicit_and_clears_pending_proposal(self):
        self.fake_engine(False)
        m = self.manager
        def write(path, content, mode=0o600):
            if path.parent == m.home / "tuning-history":
                raise OSError("history disk full")
            atomic(path, content, mode)
        with patch("flint.core.atomic", side_effect=write):
            with self.assertRaisesRegex(Problem, "history disk full"):
                m.tune()
        self.assertEqual(m.tuning["state"], "error")
        self.assertFalse(m.tuning["pending"])
        self.assertIsNone(m.tuning["proposal"])
        self.assertEqual(json.loads((m.home / "tuning.json").read_text())["state"], "error")
        self.assertEqual(m.config, DEFAULTS)
        self.assertFalse(self.live)

    def test_history_id_missing_file_and_symlinks_do_not_return_fake_success(self):
        self.fake_engine(False)
        m = self.manager
        m.tune(current_only=True)
        identifier = m.tuning["tuning_id"]
        for invalid in ("../../settings", identifier + "/../settings", "bad", "0" * 36):
            with self.assertRaises(Problem):
                m.historical_tuning(invalid)
        path = m.home / "tuning-history" / (identifier + ".json")
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            m.historical_tuning(identifier)
        path.symlink_to(m.config_file)
        with self.assertRaisesRegex(Problem, "심볼릭"):
            m.historical_tuning(identifier)
