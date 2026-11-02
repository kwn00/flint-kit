import copy
import json
import threading
from unittest.mock import Mock, patch

from flint.core import Cancelled, DEFAULTS, EngineStartError, Manager, Problem, atomic
import test_flint


class ProposalTests(test_flint.Fixture):
    fake_engine = test_flint.TunerTests.fake_engine

    def pending(self, running=True):
        self.fake_engine(running)
        self.manager.tune()
        return {"tuning_id": self.manager.tuning["tuning_id"]}

    def test_progress_publishes_all_steps_and_incremental_results(self):
        self.fake_engine()
        self.manager.proc = Mock(pid=99999)
        probe = self.manager.probe
        seen = []
        engine_steps = []
        start = self.manager.start
        def capture_start(config):
            engine_steps.append(copy.deepcopy(self.manager.tuning))
            start(config)
        self.manager.start = capture_start
        def capture(*args, **kwargs):
            seen.append(copy.deepcopy(self.manager.status()["tuning"]))
            return probe(*args, **kwargs)
        self.manager.probe = capture
        self.manager.tune()
        details = {row["progress"]["detail"] for row in seen}
        self.assertTrue({"기준 응답 재현성 1/3", "기준 응답 재현성 3/3", "출력 재현성 3/3",
                         "출력 검증", "워밍업", "짧은 생성 1/3", "짧은 생성 3/3",
                         "긴 생성 1/3", "긴 생성 3/3", "읽기 1/3", "읽기 3/3"} <= details)
        self.assertTrue({"verify_baseline", "verify_candidate"} <= {row["progress"]["phase"] for row in seen})
        self.assertTrue(any(row["results"] for row in seen))
        self.assertEqual(engine_steps[0]["progress"]["detail"], "엔진 시작")
        self.assertEqual(engine_steps[-1]["progress"]["phase"], "restore")
        self.assertEqual(engine_steps[-1]["state"], "running")
        self.assertFalse(engine_steps[-1]["pending"])
        self.assertIsNone(seen[0]["progress"]["eta_seconds"])
        self.assertTrue(any(row["progress"]["eta_seconds"] is not None for row in seen))
        p = self.manager.tuning["progress"]
        self.assertEqual((p["completed"], p["total"]), (7, 7))
        self.assertGreaterEqual(p["updated_at"], p["started_at"])

    def test_pending_survives_restart_and_settings_bytes_restore(self):
        m = self.manager
        m.save(m.config)
        before = m.config_file.read_bytes()
        self.pending()
        self.assertEqual(m.config_file.read_bytes(), before)
        restored = Manager(m.home, detector=self.detector)
        self.assertTrue(restored.tuning["pending"])
        self.assertFalse(restored.tuning["applied"])
        self.assertNotEqual(restored.config, restored.tuning["proposal"])
        self.assertTrue(restored.status()["capabilities"]["tune_apply"])
        self.assertTrue(restored.status()["capabilities"]["tune_reliability"])
        self.assertEqual(restored.tuning["verification"]["state"], "passed")

    def test_unchanged_has_no_pending(self):
        m = self.manager
        m.save({**m.config, "cache_gb": 16})
        self.fake_engine()
        probe = m.probe
        def equal(*args, **kwargs):
            row = probe(*args, **kwargs)
            if "decode_tokens_per_second" in row:
                row["decode_tokens_per_second"] = 100
            return row
        m.probe = equal
        m.tune()
        self.assertEqual(m.config, m.tuning["proposal"])
        self.assertFalse(m.tuning["pending"])
        self.assertEqual(m.tuning["verification"]["state"], "not_needed")
        self.assertEqual((m.tuning["progress"]["completed"], m.tuning["progress"]["total"]), (5, 5))
        with self.assertRaises(Problem):
            m.apply_tuning({"tuning_id": m.tuning["tuning_id"]})

    def test_two_percent_faster_candidate_is_not_adopted(self):
        m = self.manager
        m.save({**m.config, "cache_gb": 16})
        self.fake_engine()
        probe = m.probe
        def below_threshold(*args, **kwargs):
            row = probe(*args, **kwargs)
            if "decode_tokens_per_second" in row:
                row["decode_tokens_per_second"] = 102 if self.trial["workers"][0] == 32 else 100
            return row
        m.probe = below_threshold
        m.tune()
        scores = [r["score"] for r in m.tuning["results"] if r["phase"] == "workers"]
        self.assertEqual(scores, [100, 102])
        self.assertFalse(m.tuning["adopted"])
        self.assertFalse(m.tuning["pending"])
        self.assertEqual(m.tuning["proposal"], m.config)

    def test_apply_starts_even_previously_stopped_and_only_once(self):
        body = self.pending(False)
        m = self.manager
        self.assertFalse(self.live)
        m.apply_tuning(body)
        self.assertTrue(self.live)
        self.assertTrue(m.tuning["applied"])
        self.assertFalse(m.tuning["pending"])
        self.assertEqual(m.config, m.tuning["proposal"])
        self.assertTrue(m.tuning["cache_selection"]["applied"])
        self.assertEqual(json.loads((m.home / "tuning.json").read_text())["settings_after"], m.config)
        with self.assertRaises(Problem):
            m.apply_tuning(body)

    def test_apply_guards_leave_state_and_engine_unchanged(self):
        body = self.pending()
        m = self.manager
        initial = copy.deepcopy(m.tuning)
        starts = len(self.starts)
        invalid_bodies = [{}, {**body, "workers": [32, 32, 32]}, {"tuning_id": "wrong"}]
        for bad in invalid_bodies:
            with self.assertRaises(Problem):
                m.apply_tuning(bad)
            self.assertEqual(m.tuning, initial)
        m.config = {**m.config, "temperature": 0.5}
        with self.assertRaisesRegex(Problem, "설정이 변경"):
            m.apply_tuning(body)
        m.config = dict(initial["settings_before"])
        atomic(m.config_file, json.dumps({**m.config, "temperature": 0.7}))
        with self.assertRaisesRegex(Problem, "설정이 변경"):
            m.apply_tuning(body)
        m.config_file.unlink()
        with self.assertRaisesRegex(Problem, "설정이 변경"):
            m.apply_tuning(body)
        atomic(m.config_file, "bad json")
        with self.assertRaisesRegex(Problem, "확인할 수 없습니다"):
            m.apply_tuning(body)
        self.assertTrue(self.live)
        self.assertEqual(len(self.starts), starts)
        self.assertEqual(m.tuning, initial)

    def test_environment_cache_hardware_guards(self):
        body = self.pending()
        m = self.manager
        initial = copy.deepcopy(m.tuning)
        for key, value in (("macos", "28.0"), ("gpu_cores", 40), ("chip", "Apple M4 Max"),
                           ("memory_bytes", 64 * 2**30)):
            previous = self.hardware[key]
            self.hardware[key] = value
            with self.assertRaisesRegex(Problem, "환경이 달라"):
                m.apply_tuning(body)
            self.hardware[key] = previous
        with patch("flint.core.shutil.disk_usage", return_value=Mock(free=40 * 2**30)):
            with self.assertRaisesRegex(Problem, "캐시 용량"):
                m.apply_tuning(body)
        m.tuning["proposal"]["workers"] = [900, 32, 32]
        with self.assertRaisesRegex(Problem, "workers"):
            m.apply_tuning(body)
        m.tuning = initial
        self.assertTrue(self.live)
        self.assertEqual(m.tuning, initial)

    def test_unknown_environment_warns_but_known_workers_still_validated(self):
        body = self.pending()
        self.hardware["gpu_cores"] = None
        self.manager.tuning["proposal"]["workers"] = list(DEFAULTS["workers"])
        self.manager.tuning["verification"]["candidate"]["workers"] = list(DEFAULTS["workers"])
        with patch.object(self.manager, "log") as log:
            self.manager.apply_tuning(body)
        self.assertTrue(any("확인할 수 없는" in str(call) for call in log.call_args_list))
        self.assertTrue(self.manager.tuning["applied"])

    def test_unknown_or_unsupported_macos_is_refused_before_stopping_on_apply(self):
        body = self.pending()
        for version in (None, "14.7"):
            self.hardware["macos"] = version
            with self.subTest(version=version), patch.object(self.manager, "stop") as stop:
                with self.assertRaisesRegex(Problem, "macOS 15"):
                    self.manager.apply_tuning(body)
                stop.assert_not_called()
                self.assertTrue(self.manager.tuning["pending"])
                self.assertTrue(self.live)

    def test_concurrent_apply_refused(self):
        body = self.pending()
        entered, release = threading.Event(), threading.Event()
        start = self.manager.start
        errors = []
        def blocked(config):
            entered.set()
            release.wait(5)
            start(config)
        self.manager.start = blocked
        def apply():
            try:
                self.manager.apply_tuning(body)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=apply)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(Problem, "다른 작업"):
                self.manager.apply_tuning(body)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(errors)
        self.assertTrue(self.manager.tuning["applied"])

    def test_apply_failure_restores_stopped_state_and_retains_pending(self):
        body = self.pending(False)
        m = self.manager
        original = dict(m.config)
        with patch.object(m, "start", side_effect=Problem("load failed")):
            with self.assertRaisesRegex(Problem, "원본으로 롤백"):
                m.apply_tuning(body)
        self.assertFalse(self.live)
        self.assertEqual(m.config, original)
        self.assertTrue(m.tuning["pending"])
        self.assertFalse(m.tuning["applied"])
        self.assertIsNone(m.benchmark_token)

    def test_apply_settings_and_report_write_failures_restore(self):
        for target in ("settings", "report"):
            for persistent in (False, True):
                with self.subTest(target=target, persistent=persistent):
                    body = self.pending()
                    m = self.manager
                    original = dict(m.config)
                    failed = False
                    def write(path, content, mode=0o600):
                        nonlocal failed
                        match = path == (m.config_file if target == "settings" else m.home / "tuning.json")
                        if match and (not failed or persistent):
                            failed = True
                            raise OSError("injected write failure")
                        atomic(path, content, mode)
                    with patch("flint.core.atomic", side_effect=write):
                        with self.assertRaisesRegex(Problem, "write failure"):
                            m.apply_tuning(body)
                    self.assertTrue(self.live)
                    self.assertTrue(m.tuning["pending"])
                    self.assertFalse(m.tuning["applied"])
                    self.assertEqual(m.config, original)
                    self.assertIsNone(m.benchmark_token)
                    self.assertFalse(m.cancel.is_set())
                    self.assertEqual(self.starts[-1], original)

    def test_legacy_record_not_pending(self):
        m = self.manager
        legacy = {"state": "done", "results": [], "adopted": True}
        atomic(m.home / "tuning.json", json.dumps(legacy))
        restored = Manager(m.home, detector=self.detector)
        self.assertEqual(restored.tuning, legacy)
        with self.assertRaises(Problem):
            restored.apply_tuning({"tuning_id": "legacy"})

    def test_new_performance_proposal_requires_matching_final_verification(self):
        body = self.pending()
        m = self.manager
        report = copy.deepcopy(m.tuning)
        for state in ("not_run", "running", "error", "rejected"):
            with self.subTest(state=state):
                m.tuning["verification"]["state"] = state
                with self.assertRaisesRegex(Problem, "최종 조합"):
                    m.apply_tuning(body)
                self.assertTrue(m.tuning["pending"])
                self.assertEqual(m.config, report["settings_before"])
        m.tuning = copy.deepcopy(report)
        m.tuning["verification"]["candidate"]["chunk"] = 4096
        with self.assertRaisesRegex(Problem, "최종 조합"):
            m.apply_tuning(body)
        m.tuning = copy.deepcopy(report)
        m.tuning["baseline_check"]["state"] = "failed"
        with self.assertRaisesRegex(Problem, "재현성"):
            m.apply_tuning(body)
        self.assertTrue(self.live)

    def test_old_pending_proposal_remains_compatible_without_new_verification(self):
        body = self.pending()
        self.manager.tuning.pop("baseline_check")
        self.manager.tuning.pop("verification")
        self.manager.apply_tuning(body)
        self.assertTrue(self.manager.tuning["applied"])

    def test_failed_apply_restoration_is_explicit_not_success(self):
        body = self.pending()
        m = self.manager
        start = m.start
        def fail_proposal(config):
            if config == m.tuning["proposal"]:
                raise Problem("candidate start failed")
            start(config)
        def write(path, content, mode=0o600):
            if path == m.config_file and json.loads(content) == m.tuning["settings_before"]:
                raise OSError("rollback disk full")
            atomic(path, content, mode)
        with patch.object(m, "start", side_effect=fail_proposal), patch("flint.core.atomic", side_effect=write):
            with self.assertRaisesRegex(Problem, "복원 실패.*rollback disk full"):
                m.apply_tuning(body)
        self.assertIn("원본 설정 저장 실패", m.tuning["restoration_error"])
        self.assertTrue(m.tuning["pending"])
        self.assertFalse(m.tuning["applied"])
        self.assertTrue(self.live)
        self.assertIsNone(m.benchmark_token)

    def test_late_cancel_does_not_interrupt_restoration(self):
        self.fake_engine()
        start = self.manager.start
        late_cancels = []
        def capture(config):
            if self.manager.restoring:
                with self.assertRaisesRegex(Problem, "복원"):
                    self.manager.cancel_work()
                late_cancels.append(True)
                self.assertFalse(self.manager.cancel.is_set())
            start(config)
        self.manager.start = capture
        self.manager.tune()
        self.assertTrue(late_cancels)
        self.assertTrue(self.live)
        self.assertFalse(self.manager.restoring)


class ReliabilityTests(test_flint.Fixture):
    fake_engine = test_flint.TunerTests.fake_engine

    def assert_restored(self, running):
        m = self.manager
        self.assertEqual(m.config, DEFAULTS)
        self.assertEqual(self.live, running)
        self.assertIsNone(m.benchmark_token)
        self.assertFalse(m.cancel.is_set())
        self.assertFalse(m.tuning["pending"])
        self.assertFalse(m.tuning["adopted"])
        self.assertIsNone(m.tuning.get("proposal"))
        self.assertEqual(json.loads((m.home / "tuning.json").read_text()), m.tuning)

    def test_three_baseline_checks_precede_all_speed_probes(self):
        self.fake_engine(False)
        m = self.manager
        probe = m.probe
        m.probe = Mock(side_effect=probe)
        m.tune(current_only=True)
        calls = m.probe.call_args_list
        self.assertEqual([call.kwargs.get("check", False) for call in calls[:4]], [True, True, True, False])
        self.assertTrue(all(call.kwargs["temperature"] == 0 for call in calls[:3]))
        self.assertEqual(m.tuning["baseline_check"], dict(state="passed", required=3, completed=3))
        self.assertEqual(m.tuning["results"][0]["answer_hashes"], ["same"] * 3)
        self.assertEqual(len(m.tuning["results"]), 1)
        self.assertEqual(m.tuning["verification"]["state"], "not_needed")
        self.assertNotIn("cache_selection", m.tuning)
        self.assertEqual(m.config, DEFAULTS)
        self.assertFalse(self.live)

    def test_nonrepeatable_baseline_aborts_before_candidates_or_speed_measurements(self):
        for running in (False, True):
            for mismatch_at in (2, 3):
                with self.subTest(running=running, mismatch_at=mismatch_at):
                    self.fake_engine(running)
                    m = self.manager
                    hashes = ["same"] * (mismatch_at - 1) + ["different"]
                    m.probe = Mock(side_effect=[{"answer_sha256": value} for value in hashes])
                    with self.assertRaisesRegex(Problem, "재현성"):
                        m.tune()
                    self.assertEqual(m.probe.call_count, mismatch_at)
                    self.assertTrue(all(call.kwargs.get("check") for call in m.probe.call_args_list))
                    self.assertEqual(m.tuning["baseline_check"]["state"], "failed")
                    self.assertEqual(m.tuning["baseline_check"]["completed"], mismatch_at)
                    self.assertEqual(m.tuning["results"][0]["answer_hashes"], hashes)
                    self.assertEqual(len(self.starts), 2 if running else 1)
                    self.assertNotIn("cache_selection", m.tuning)
                    self.assert_restored(running)

    def test_candidate_answer_mismatch_is_not_retried_or_adopted(self):
        self.fake_engine()
        m = self.manager
        probe = m.probe
        def changed_answer(*args, **kwargs):
            if kwargs.get("check") and self.trial["workers"][0] == 32:
                return {"answer_sha256": "different"}
            return probe(*args, **kwargs)
        m.probe = changed_answer
        m.tune()
        rejected = m.tuning["results"][1]
        self.assertIsNone(rejected["score"])
        self.assertIn("불일치", rejected["error"])
        self.assertEqual(rejected["start_attempts"], 1)
        self.assertEqual(rejected["start_errors"], [])
        self.assertEqual(m.tuning["verification"]["state"], "not_needed")
        self.assertFalse(m.tuning["adopted"])
        self.assertEqual(m.tuning["proposal"]["workers"], DEFAULTS["workers"])
        self.assertEqual(len(m.tuning["results"]), 5)

    def test_retry_once_in_baseline_and_final_candidate_with_errors_preserved(self):
        for phase in ("workers", "verify_candidate"):
            with self.subTest(phase=phase):
                self.fake_engine(False)
                m = self.manager
                start = m.start
                failed = False
                def transient(config):
                    nonlocal failed
                    self.assertFalse(self.live)
                    if m.tuning["progress"]["phase"] == phase and not failed:
                        failed = True
                        self.live = True
                        raise EngineStartError("transient startup exit")
                    start(config)
                m.start = transient
                with patch.object(m.cancel, "wait", return_value=False) as wait:
                    m.tune()
                    wait.assert_called_once_with(1)
                retried = next(row for row in m.tuning["results"] if row["phase"] == phase)
                self.assertEqual(retried["start_attempts"], 2)
                self.assertEqual(retried["start_errors"], ["transient startup exit"])
                self.assertIsNone(retried["error"])
                self.assertEqual(m.tuning["verification"]["state"], "passed")
                self.assertTrue(m.tuning["pending"])
                self.assertFalse(self.live)
                self.assertEqual(m.config, DEFAULTS)

    def test_two_startup_failures_abort_without_third_attempt(self):
        self.fake_engine(False)
        m = self.manager
        m.start = Mock(side_effect=EngineStartError("not ready"))
        m.probe = Mock()
        with patch.object(m.cancel, "wait", return_value=False):
            with self.assertRaisesRegex(Problem, "not ready"):
                m.tune()
        self.assertEqual(m.start.call_count, 2)
        m.probe.assert_not_called()
        self.assertEqual(m.tuning["results"][0]["start_errors"], ["not ready", "not ready"])
        self.assertEqual(m.tuning["results"][0]["start_attempts"], 2)
        self.assert_restored(False)

    def test_preflight_errors_and_cancellation_are_never_retried(self):
        for error in (Problem("invalid settings"), Problem("model integrity failed"),
                      OSError("permission denied"), ValueError("invalid input"), Cancelled("cancel")):
            with self.subTest(error=error):
                self.fake_engine(False)
                m = self.manager
                m.start = Mock(side_effect=error)
                m.probe = Mock()
                with self.assertRaises((Problem, OSError, ValueError)):
                    m.tune()
                m.start.assert_called_once()
                m.probe.assert_not_called()
                self.assert_restored(False)

    def test_bad_measurement_does_not_enter_start_retry(self):
        self.fake_engine(False)
        m = self.manager
        start = m.start
        m.start = Mock(side_effect=start)
        m.probe = Mock(side_effect=EngineStartError("measurement failed, not startup"))
        with self.assertRaisesRegex(Problem, "measurement failed"):
            m.tune()
        m.start.assert_called_once()
        self.assertEqual(m.tuning["results"][0]["start_errors"], [])
        self.assert_restored(False)

    def test_overflowed_aggregate_is_failed_without_nonfinite_json_values(self):
        for metric in ("decode", "prefill"):
            with self.subTest(metric=metric):
                self.fake_engine(False)
                m = self.manager
                probe = m.probe
                def overflow(*args, **kwargs):
                    row = probe(*args, **kwargs)
                    if not kwargs.get("check"):
                        if metric == "decode":
                            row["decode_tokens_per_second"] = 1e308
                        else:
                            row.update(prefilled_tokens=1e308, prefill_seconds=1e-308)
                    return row
                m.probe = overflow
                with self.assertRaisesRegex(Problem, "유효한"):
                    m.tune()
                self.assertIsNone(m.tuning["results"][0]["score"])
                self.assertEqual(m.tuning["results"][0]["start_attempts"], 1)
                json.dumps(m.tuning, allow_nan=False)
                self.assert_restored(False)

    def test_cancellation_during_start_retry_wait_stops_second_attempt(self):
        self.fake_engine(False)
        m = self.manager
        def cancel(config):
            m.cancel.set()
            raise EngineStartError("temporary startup failure")
        m.start = Mock(side_effect=cancel)
        with self.assertRaises(Cancelled):
            m.tune()
        m.start.assert_called_once()
        self.assertEqual(m.tuning["state"], "cancelled")
        self.assert_restored(False)

    def test_final_worker_comparison_uses_fresh_baseline_and_exact_thresholds(self):
        cases = [
            ("improved", 100, 104, 4000, True),
            ("exact_margin", 100, 103, 4000, False),
            ("above_margin", 100, 103.000001, 4000, True),
            ("within_regression_band", 100, 104, 3880, True),
            ("excess_prefill_regression", 100, 104, 3879.99, False),
            ("lost_improvement", 100, 102, 4000, False),
            ("fresh_baseline_not_original_score", 200, 204, 4000, False),
            ("fresh_baseline_improves", 200, 208, 4000, True),
        ]
        for name, baseline, decode, prefill, passed in cases:
            with self.subTest(name=name):
                self.fake_engine(False)
                m = self.manager
                m.save({**DEFAULTS, "cache_gb": 16})
                self.live = True
                probe = m.probe
                def compare(*args, **kwargs):
                    row = probe(*args, **kwargs)
                    phase = m.tuning["progress"]["phase"]
                    if not kwargs.get("check"):
                        if phase == "verify_baseline":
                            row["decode_tokens_per_second"] = baseline
                        elif phase == "verify_candidate":
                            row.update(decode_tokens_per_second=decode,
                                       prefilled_tokens=prefill, prefill_seconds=1)
                    return row
                m.probe = compare
                m.tune()
                report = m.tuning
                self.assertEqual(report["verification"]["state"], "passed" if passed else "rejected")
                self.assertEqual(report["adopted"], passed)
                self.assertEqual(report["pending"], passed)
                self.assertEqual(report["proposal"]["workers"], [32, 32, 32] if passed else DEFAULTS["workers"])
                self.assertEqual(report["results"][-2]["phase"], "verify_baseline")
                self.assertEqual(report["results"][-2]["decode_score"], baseline)
                self.assertEqual(report["results"][-1]["phase"], "verify_candidate")
                self.assertEqual(report["results"][-1]["decode_score"], decode)
                self.assertTrue(all(len(row["answer_hashes"]) == 3 for row in report["results"][-2:]))
                self.assertTrue(all(len(row["rounds"]) == 6 and len(row["prefill_rounds"]) == 3
                                    for row in report["results"][-2:]))
                self.assertEqual(self.starts[-3]["workers"], DEFAULTS["workers"])
                self.assertEqual(self.starts[-2]["workers"], [32, 32, 32])
                self.assertEqual(self.starts[-1], m.config)
                self.assertTrue(self.live)
                self.assertEqual(m.config, {**DEFAULTS, "cache_gb": 16})
                self.assertEqual(json.loads((m.home / "tuning.json").read_text()),
                                 json.loads(json.dumps(report, allow_nan=False)))

    def test_chunk_and_combined_changes_require_their_own_final_improvements(self):
        cases = [
            ("chunk_only", False, 100, 5000, True),
            ("chunk_exact_margin", False, 100, 4120, False),
            ("chunk_above_margin", False, 100, 4120.000001, True),
            ("chunk_decode_regression_edge", False, 97, 5000, True),
            ("chunk_decode_regression", False, 96.99, 5000, False),
            ("both_improve", True, 104, 5000, True),
            ("both_require_prefill_improvement", True, 104, 4080, False),
            ("both_require_decode_improvement", True, 102, 5000, False),
        ]
        for name, workers_changed, decode, prefill, passed in cases:
            with self.subTest(name=name):
                self.fake_engine(False)
                m = self.manager
                m.save({**DEFAULTS, "cache_gb": 16})
                probe = m.probe
                def compare(*args, **kwargs):
                    row = probe(*args, **kwargs)
                    if not kwargs.get("check"):
                        row.update(decode_tokens_per_second=104 if workers_changed and self.trial["workers"][0] == 32 else 100,
                                   prefilled_tokens=5000 if self.trial["chunk"] == 4096 else 4000,
                                   prefill_seconds=1)
                        if m.tuning["progress"]["phase"] == "verify_candidate":
                            row.update(decode_tokens_per_second=decode, prefilled_tokens=prefill)
                    return row
                m.probe = compare
                m.tune()
                self.assertEqual(m.tuning["verification"]["state"], "passed" if passed else "rejected")
                self.assertEqual(m.tuning["adopted"], passed)
                self.assertEqual(m.tuning["pending"], passed)
                self.assertEqual(m.tuning["proposal"]["chunk"], 4096 if passed else DEFAULTS["chunk"])
                self.assertEqual(m.tuning["proposal"]["workers"],
                                 [32, 32, 32] if passed and workers_changed else DEFAULTS["workers"])
                self.assertFalse(self.live)
                self.assertEqual(m.config, {**DEFAULTS, "cache_gb": 16})

    def test_failed_final_comparison_can_only_propose_independent_cache_size(self):
        self.fake_engine()
        m = self.manager
        probe = m.probe
        def no_final_gain(*args, **kwargs):
            row = probe(*args, **kwargs)
            if not kwargs.get("check") and m.tuning["progress"]["phase"] == "verify_candidate":
                row["decode_tokens_per_second"] = 102
            return row
        m.probe = no_final_gain
        m.tune()
        self.assertEqual(m.tuning["verification"]["state"], "rejected")
        self.assertTrue(m.tuning["pending"])
        self.assertFalse(m.tuning["adopted"])
        self.assertEqual(m.tuning["proposal"], {**DEFAULTS, "cache_gb": 16})
        m.apply_tuning({"tuning_id": m.tuning["tuning_id"]})
        self.assertEqual(m.config, {**DEFAULTS, "cache_gb": 16})
        self.assertFalse(m.config["disk_cache"])

    def test_final_errors_and_cancellation_restore_without_any_proposal(self):
        for phase in ("verify_baseline", "verify_candidate"):
            for running in (False, True):
                for error in (Problem("bad final metrics"), Cancelled("cancel final")):
                    with self.subTest(phase=phase, running=running, error=error):
                        self.fake_engine(running)
                        m = self.manager
                        probe = m.probe
                        def failed(*args, **kwargs):
                            if m.tuning["progress"]["phase"] == phase and not kwargs.get("check"):
                                raise error
                            return probe(*args, **kwargs)
                        m.probe = failed
                        with self.assertRaisesRegex(Problem, "final"):
                            m.tune()
                        self.assertEqual(m.tuning["verification"]["state"],
                                         "cancelled" if isinstance(error, Cancelled) else "error")
                        self.assertIn("final", m.tuning["verification"]["reason"])
                        self.assert_restored(running)

    def test_restarted_final_baseline_and_candidate_must_still_match_reference_answer(self):
        for phase in ("verify_baseline", "verify_candidate"):
            with self.subTest(phase=phase):
                self.fake_engine()
                m = self.manager
                probe = m.probe
                checks = 0
                def different(*args, **kwargs):
                    nonlocal checks
                    if m.tuning["progress"]["phase"] == phase and kwargs.get("check"):
                        checks += 1
                        if checks == 2:
                            return {"answer_sha256": "different"}
                    return probe(*args, **kwargs)
                m.probe = different
                with self.assertRaisesRegex(Problem, "불일치"):
                    m.tune()
                self.assertEqual(m.tuning["verification"]["state"], "error")
                self.assertEqual(m.tuning["results"][-1]["answer_hashes"], ["same", "different"])
                self.assertEqual(m.tuning["results"][-1]["start_attempts"], 1)
                self.assertEqual(m.tuning["results"][-1]["start_errors"], [])
                self.assert_restored(True)
