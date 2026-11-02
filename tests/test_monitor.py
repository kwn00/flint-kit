import json
from pathlib import Path
import subprocess
from unittest.mock import Mock, patch

from flint.monitor import request_series, SERIES_INTERVALS
from test_flint import Fixture

NOW = 1_700_000_160
NEWEST = NOW // 300 * 300
FIRST = NEWEST - 11 * 300
EMPTY_SLOTS = [dict(start=FIRST + 300 * index, requests=0, decode=None, prefill=None, ttft=None)
               for index in range(12)]


def done(at, **values):
    return dict(status="complete", time=at, **values)


class MonitorTests(Fixture):
    def test_tree_rss_units_sampling_and_stop(self):
        monitor = self.manager.monitor
        proc = Mock(pid=100)
        proc.poll.return_value = None
        result = Mock(returncode=0, stdout="100 1 1024\n101 100 2048\n102 101 512\n999 1 999999\n")
        with patch("flint.monitor.subprocess.run", return_value=result) as run, \
                patch("flint.monitor.time.monotonic", return_value=10):
            memory = monitor.snapshot(proc)["memory"]
            self.assertEqual(memory["rss_bytes"], 3584 * 1024)
            self.assertEqual(memory["process_count"], 3)
            self.assertEqual(memory["sampled_peak_bytes"], memory["rss_bytes"])
            self.assertEqual(monitor.snapshot(proc)["memory"], memory)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["/bin/ps", "-axo", "pid=,ppid=,rss="])
        proc.poll.return_value = 0
        self.assertIsNone(monitor.snapshot(proc)["memory"]["rss_bytes"])
        monitor.begin()
        self.assertIsNone(monitor.snapshot(None)["memory"]["sampled_peak_bytes"])

    def test_sampling_failure_never_reuses_current_rss(self):
        monitor = self.manager.monitor
        proc = Mock(pid=100)
        proc.poll.return_value = None
        for result in [Mock(returncode=1), Mock(returncode=0, stdout="invalid"),
                       Mock(returncode=0, stdout="999 1 9999\n")]:
            monitor.begin()
            with patch("flint.monitor.subprocess.run", return_value=result):
                value = monitor.snapshot(proc)["memory"]
                self.assertEqual(value["state"], "unavailable")
                self.assertIsNone(value["rss_bytes"])
                self.assertTrue(value["error"])
        monitor.begin()
        with patch("flint.monitor.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertEqual(monitor.snapshot(proc)["memory"]["state"], "unavailable")

    def test_errors_are_bounded_classified_and_private(self):
        monitor = self.manager.monitor
        log = self.manager.home / "engine.log"
        log.write_text("ValueError: model PRIVATE-PROMPT\n")
        monitor.begin()
        with log.open("a") as out:
            out.write("MemoryError: secret-api-key\nTraceback (most recent call last):\n")
        report = monitor.snapshot(None)
        self.assertEqual(report["errors"], [
            {"category": "메모리 할당 오류", "lines": 1}, {"category": "엔진 실행 오류", "lines": 1}])
        self.assertNotIn("PRIVATE-PROMPT", json.dumps(report))
        self.assertNotIn("secret-api-key", json.dumps(report))
        with log.open("a") as out:
            out.write("normal\n" * 20000 + "TimeoutError: PRIVATE-RESPONSE\n")
        self.assertEqual(monitor.snapshot(None)["errors"], [{"category": "처리 시간 초과", "lines": 1}])

    def test_rotated_log_and_read_failure(self):
        monitor = self.manager.monitor
        log = self.manager.home / "engine.log"
        log.write_text("old\n" * 20)
        monitor.begin()
        log.rename(log.with_suffix(".previous"))
        log.write_text("RuntimeError: Address already in use\n")
        self.assertEqual(monitor.snapshot(None)["errors"], [{"category": "포트·연결 관련 오류", "lines": 1}])
        with patch("pathlib.Path.open", side_effect=PermissionError):
            self.assertTrue(monitor.snapshot(None)["error_read_failure"])

    def test_metrics_preserve_real_runtime_status_and_reject_nonfinite(self):
        data = dict(time=100, status="cancelled_or_failed", prompt_tokens=50, token_ids=[42],
                    prompt="PRIVATE-PROMPT", sampling={"key": "PRIVATE-KEY"},
                    ttft_seconds=float("nan"), decode_tokens_per_second=float("inf"), huge=10**400)
        (self.manager.home / "metrics.jsonl").write_text(json.dumps(data) + "\n")
        self.assertEqual(self.manager.metrics(), [dict(time=100, status="cancelled_or_failed", prompt_tokens=50)])
        state = self.manager.status()
        self.assertTrue(state["capabilities"]["engine_monitor"])
        self.assertEqual(state["monitor"]["memory"]["state"], "stopped")

    def test_engine_exit_clears_ready_and_reports_code(self):
        proc = Mock(pid=100)
        proc.poll.return_value = -9
        self.manager.proc = proc
        self.manager.ready = True
        state = self.manager.status()
        self.assertFalse(state["engine"]["running"])
        self.assertFalse(state["engine"]["ready"])
        self.assertEqual(state["engine"]["exit_code"], -9)
        self.assertIsNone(state["monitor"]["memory"]["rss_bytes"])


class RequestSeriesTests(Fixture):
    def write(self, rows, tail=b""):
        path = self.manager.home / "metrics.jsonl"
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows) + tail)
        return path

    def test_each_interval_changes_the_window_and_keeps_twelve_clock_slots(self):
        self.assertEqual(SERIES_INTERVALS, (5, 60, 300))
        for seconds in SERIES_INTERVALS:
            with self.subTest(seconds=seconds):
                newest = NOW // seconds * seconds
                first = newest - 11 * seconds
                path = self.write([
                    done(first - 1, ttft_seconds=9), done(first, ttft_seconds=1),
                    done(first + seconds - 0.1, ttft_seconds=3),
                    done(first + seconds, ttft_seconds=5), done(newest, ttft_seconds=7),
                    done(newest + seconds, ttft_seconds=9)])
                series = request_series(path, 0, NOW, seconds=seconds)
                self.assertEqual(series["bucket_seconds"], seconds)
                self.assertEqual([b["start"] for b in series["buckets"]],
                                 [first + seconds * i for i in range(12)])
                self.assertEqual([b["requests"] for b in series["buckets"]], [2, 1] + [0] * 9 + [1])
                self.assertEqual([b["ttft"] for b in series["buckets"]], [2, 5] + [None] * 9 + [7])
                current = request_series(path, newest, NOW, seconds=seconds)
                self.assertEqual([b["requests"] for b in current["buckets"]], [0] * 11 + [1])

    def test_interval_caches_are_independent_bounded_and_reset_on_run(self):
        monitor = self.manager.monitor
        proc = Mock(pid=100)
        proc.poll.return_value = None
        clock = [NOW // 300 * 300 + 10]
        with patch("flint.monitor.subprocess.run", return_value=Mock(returncode=0, stdout="100 1 1024\n")), \
                patch("flint.monitor.time.time", side_effect=lambda: clock[0]), \
                patch("flint.monitor.request_series", wraps=request_series) as build:
            monitor.begin()
            for _ in range(2):
                for seconds in SERIES_INTERVALS:
                    self.assertEqual(monitor.snapshot(proc, seconds)["request_series"]["bucket_seconds"], seconds)
            self.assertEqual(build.call_count, 3, "alternating tabs reuse each interval's cache")
            clock[0] += 5
            for seconds in SERIES_INTERVALS:
                monitor.snapshot(proc, seconds)
            self.assertEqual(build.call_count, 4, "a five-second rollover leaves the minute-series caches intact")
            self.write([done(clock[0], ttft_seconds=2)])
            for seconds in SERIES_INTERVALS:
                self.assertEqual(monitor.snapshot(proc, seconds)["request_series"]["buckets"][-1]["ttft"], 2)
            self.assertEqual(build.call_count, 7, "appending a request invalidates each cached series")
            clock[0] += 1
            monitor.begin()
            self.assertEqual(len(monitor.series_cache), 0)
            for seconds in SERIES_INTERVALS:
                series = monitor.snapshot(proc, seconds)["request_series"]
                self.assertEqual(sum(b["requests"] for b in series["buckets"]), 0)
            self.assertEqual(build.call_count, 10)
            self.assertEqual(len(monitor.series_cache), 3)
            proc.poll.return_value = 0
            for seconds in SERIES_INTERVALS:
                self.assertIsNone(monitor.snapshot(proc, seconds)["request_series"])
            for invalid in (0, -60, 1, 900, 3600, "5", 5.0, "60", 60.0, True, None):
                with self.assertRaisesRegex(ValueError, "5, 60, 300"):
                    monitor.snapshot(proc, invalid)
            self.assertEqual(build.call_count, 10)

    def test_five_second_series_rolls_and_expires_without_new_requests(self):
        monitor = self.manager.monitor
        proc = Mock(pid=100)
        proc.poll.return_value = None
        clock = [NOW]
        with patch("flint.monitor.subprocess.run", return_value=Mock(returncode=0, stdout="100 1 1024\n")), \
                patch("flint.monitor.time.time", side_effect=lambda: clock[0]):
            monitor.begin()
            clock[0] += 2
            self.write([done(NOW + 1, ttft_seconds=1.5)])
            first = monitor.snapshot(proc, 5)["request_series"]
            self.assertEqual(first["buckets"][-1]["ttft"], 1.5)
            self.assertEqual(first["buckets"][-1]["start"], NOW)
            clock[0] = NOW + 5
            rolled = monitor.snapshot(proc, 5)["request_series"]
            self.assertEqual(rolled["buckets"][-2]["ttft"], 1.5)
            self.assertEqual(rolled["buckets"][-1]["requests"], 0)
            self.assertEqual(rolled["buckets"][-1]["start"], NOW + 5)
            clock[0] = NOW + 60
            expired = monitor.snapshot(proc, 5)["request_series"]
            self.assertTrue(all(b["requests"] == 0 and b["ttft"] is None for b in expired["buckets"]))

    def test_slots_follow_the_clock_and_the_current_run(self):
        path = self.write([
            done(FIRST - 1, ttft_seconds=9), done(FIRST, ttft_seconds=1), done(FIRST + 299.5, ttft_seconds=2),
            done(FIRST + 300, ttft_seconds=3), done(NEWEST + 299, ttft_seconds=4),
            done(NEWEST + 300, ttft_seconds=9), dict(status="cancelled_or_failed", time=FIRST + 10, ttft_seconds=9)])
        series = request_series(path, 0, NOW)
        self.assertEqual(series["bucket_seconds"], 300)
        self.assertEqual([b["start"] for b in series["buckets"]], [FIRST + 300 * index for index in range(12)])
        self.assertEqual([b["requests"] for b in series["buckets"]], [2, 1] + [0] * 9 + [1])
        self.assertEqual([b["ttft"] for b in series["buckets"]], [1.5, 3] + [None] * 9 + [4])
        started = FIRST + 650
        path = self.write([done(started - 1, ttft_seconds=1), done(started, ttft_seconds=2)])
        self.assertEqual([b["requests"] for b in request_series(path, started, NOW)["buckets"]],
                         [0, 0, 1] + [0] * 9, "requests from before this engine run are not charted")

    def test_aggregates_are_weighted_and_skip_non_measurements(self):
        reply = dict(decode_tokens_per_second=0, decode_seconds=0.0001, prefilled_tokens=0, prefill_seconds=0.001)
        path = self.write([
            done(FIRST + 1, decode_tokens_per_second=100, decode_seconds=1, prefilled_tokens=100,
                 prefill_seconds=1, ttft_seconds=1),
            done(FIRST + 2, decode_tokens_per_second=50, decode_seconds=3, prefilled_tokens=300,
                 prefill_seconds=1, ttft_seconds=5),
            done(FIRST + 3, ttft_seconds=3, **reply),
            done(FIRST + 301, ttft_seconds=0, **reply),
            done(FIRST + 601, ttft_seconds=1), done(FIRST + 602, ttft_seconds=3),
            done(FIRST + 603, ttft_seconds=5), done(FIRST + 604, ttft_seconds=7)])
        mixed, replies, even = request_series(path, 0, NOW)["buckets"][:3]
        self.assertEqual((mixed["requests"], mixed["decode"], mixed["prefill"], mixed["ttft"]), (3, 62.5, 200, 3),
                         "decode is weighted by decode time, prefill ignores cached prompts, TTFT is the median")
        self.assertEqual((replies["requests"], replies["decode"], replies["prefill"], replies["ttft"]), (1, None, None, 0),
                         "one-token replies and cached prompts have no speed, but a real zero wait stays zero")
        self.assertEqual((even["requests"], even["ttft"]), (4, 4))

    def test_invalid_values_and_lines_never_reach_the_chart(self):
        invalid = [True, "5", None, -1, float("nan"), float("inf"), 10 ** 400, [1], {"x": 1}]
        rows = [done(FIRST + 10, decode_tokens_per_second=value, decode_seconds=1, prefilled_tokens=value,
                     prefill_seconds=1, ttft_seconds=value) for value in invalid]
        rows += [done(value, ttft_seconds=1) for value in [True, "100", None, -5, float("nan"), float("inf"), 10 ** 400]]
        rows += [dict(time=FIRST + 10, ttft_seconds=1), dict(status="running", time=FIRST + 10, ttft_seconds=1)]
        hostile = b"\n".join([b"not json", b"[1]", b'"text"', b"null", b"5", b"[" * 200000,
                              b'{"status": "complete", "time": '])
        series = request_series(self.write(rows, hostile), 0, NOW)
        self.assertEqual(series["buckets"][0], dict(start=FIRST, requests=len(invalid),
                                                   decode=None, prefill=None, ttft=None))
        self.assertEqual(sum(b["requests"] for b in series["buckets"]), len(invalid))

    def test_extreme_values_stay_json_safe_per_metric(self):
        huge = dict(decode_tokens_per_second=1e308, decode_seconds=1e308, prefilled_tokens=10 ** 308,
                    prefill_seconds=1.0, ttft_seconds=1e308)
        bucket = request_series(self.write([done(FIRST + 1, **huge), done(FIRST + 2, **huge)]), 0, NOW)["buckets"][0]
        json.dumps(bucket, allow_nan=False)
        self.assertEqual((bucket["requests"], bucket["decode"], bucket["prefill"], bucket["ttft"]), (2, None, None, 1e308))

    def test_missing_or_unreadable_file_gives_empty_slots(self):
        path = self.manager.home / "metrics.jsonl"
        self.assertEqual(request_series(path, 0, NOW)["buckets"], EMPTY_SLOTS)
        path.write_bytes(b"")
        self.assertEqual(request_series(path, 0, NOW)["buckets"], EMPTY_SLOTS)
        path.write_text(json.dumps(done(FIRST, ttft_seconds=1)) + "\n")
        with patch("pathlib.Path.open", side_effect=PermissionError):
            self.assertEqual(request_series(path, 0, NOW)["buckets"], EMPTY_SLOTS)

    def test_long_logs_are_read_back_only_as_far_as_the_window_needs(self):
        padding = "x" * 700
        recent = [done(FIRST + index * 1.5, ttft_seconds=1, note=padding) for index in range(2000)]
        path = self.write(recent)
        self.assertGreater(path.stat().st_size, 2 << 18)
        series = request_series(path, 0, NOW)
        self.assertEqual(sum(b["requests"] for b in series["buckets"]), 2000)
        self.assertEqual(series["buckets"][0]["requests"], 200, "slots far from the end of the file are still counted")
        with patch("flint.monitor.SERIES_READ_LIMIT", 1 << 19):
            capped = request_series(path, 0, NOW)["buckets"]
        self.assertEqual(capped[0]["requests"], 0)
        self.assertGreater(sum(b["requests"] for b in capped), 0, "the read cap drops the oldest slots, not everything")
        old = [done(FIRST - 3600 + index, ttft_seconds=1, note=padding) for index in range(2000)]
        path = self.write(old + [done(NEWEST + 1, ttft_seconds=2)])
        opened = []
        real_open = Path.open

        def counting_open(target, *args, **kwargs):
            opened.append(target)
            return real_open(target, *args, **kwargs)
        with patch.object(Path, "open", counting_open):
            series = request_series(path, NEWEST, NOW)
        self.assertEqual([b["requests"] for b in series["buckets"]], [0] * 11 + [1])
        self.assertEqual(len(opened), 1, "a log full of older runs is read once")

    def test_monitor_caches_until_file_slot_or_run_changes_and_stops_with_engine(self):
        monitor = self.manager.monitor
        path = self.manager.home / "metrics.jsonl"
        proc = Mock(pid=100)
        proc.poll.return_value = None
        clock = [NOW]
        with patch("flint.monitor.subprocess.run", return_value=Mock(returncode=0, stdout="100 1 1024\n")), \
                patch("flint.monitor.time.time", side_effect=lambda: clock[0]), \
                patch("flint.monitor.request_series", wraps=request_series) as build:
            self.assertIsNone(monitor.snapshot(proc)["request_series"], "no run has begun")
            monitor.begin()
            self.assertEqual(monitor.snapshot(proc)["request_series"]["buckets"], EMPTY_SLOTS)
            monitor.snapshot(proc)
            self.assertEqual(build.call_count, 1, "an unchanged file in the same slot is not parsed again")
            path.write_text(json.dumps(done(NOW + 5, ttft_seconds=2)) + "\n")
            clock[0] = NOW + 10
            self.assertEqual(monitor.snapshot(proc)["request_series"]["buckets"][11]["ttft"], 2)
            monitor.snapshot(proc)
            self.assertEqual(build.call_count, 2)
            clock[0] = NEWEST + 301
            self.assertEqual(monitor.snapshot(proc)["request_series"]["buckets"][10]["ttft"], 2,
                             "a new slot moves the bars left")
            self.assertEqual(build.call_count, 3)
            monitor.begin()
            self.assertEqual(monitor.snapshot(proc)["request_series"]["buckets"][10]["requests"], 0,
                             "a new engine run starts with an empty chart")
            self.assertEqual(build.call_count, 4)
            proc.poll.return_value = 0
            self.assertIsNone(monitor.snapshot(proc)["request_series"])
            self.assertIsNone(monitor.snapshot(None)["request_series"])
            self.assertEqual(build.call_count, 4)

    def test_status_reports_the_series_for_the_running_engine_only(self):
        proc = Mock(pid=100)
        proc.poll.return_value = None
        self.manager.proc = proc
        with patch("flint.monitor.subprocess.run", return_value=Mock(returncode=0, stdout="100 1 1024\n")), \
                patch("flint.monitor.time.time", return_value=NOW):
            self.manager.monitor.begin()
            (self.manager.home / "metrics.jsonl").write_text(
                json.dumps(done(NOW + 1, ttft_seconds=1.5, decode_tokens_per_second=40, decode_seconds=2)) + "\n")
            series = self.manager.status()["monitor"]["request_series"]
            self.assertEqual(series["buckets"][11]["requests"], 1)
            self.assertEqual(series["buckets"][11]["decode"], 40)
            json.dumps(series, allow_nan=False)
            original_settings = dict(self.manager.config)
            for seconds in SERIES_INTERVALS:
                state = self.manager.status(chart_seconds=seconds)
                self.assertTrue(state["capabilities"]["chart_intervals"])
                self.assertEqual(state["capabilities"]["chart_interval_seconds"], (5, 60, 300))
                self.assertEqual(state["monitor"]["request_series"]["bucket_seconds"], seconds)
                self.assertEqual(state["monitor"]["request_series"]["buckets"][-1]["decode"], 40)
                self.assertIs(self.manager.proc, proc)
                self.assertEqual(self.manager.config, original_settings)
            proc.poll.return_value = 0
            self.assertIsNone(self.manager.status()["monitor"]["request_series"])
