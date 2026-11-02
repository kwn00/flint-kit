"""Bounded, read-only engine diagnostics. Never expose raw engine output."""
from pathlib import Path
import json
import os
import re
import subprocess
import threading
import time

SERIES_BUCKETS = 12
SERIES_BUCKET_SECONDS = 300
SERIES_INTERVALS = (5, 60, 300)
SERIES_READ_LIMIT = 8 << 20


def _number(value):
    # Bounds are compared instead of calling isfinite(): huge JSON integers must not raise.
    return type(value) in (int, float) and 0 <= value <= 1e308


def _finite(value):
    return value if value is not None and abs(value) <= 1e308 else None


def _median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else values[middle - 1] + (values[middle] - values[middle - 1]) / 2


def _ratio(parts):
    if not parts:
        return None
    try:
        return _finite(sum(top for top, _ in parts) / sum(bottom for _, bottom in parts))
    except OverflowError:
        return None


def _completed_requests(path, since):
    """Complete requests, oldest first. Reads further back only while the window is not covered."""
    limit = 1 << 18
    while True:
        try:
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                offset = max(0, size - limit)
                stream.seek(offset)
                data = stream.read(limit)
        except OSError:
            return []
        if offset:
            data = data.partition(b"\n")[2]
        rows = []
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(row, dict) and row.get("status") == "complete" and _number(row.get("time")):
                rows.append(row)
        if not offset or limit >= SERIES_READ_LIMIT or (rows and rows[0]["time"] < since):
            return rows
        limit *= 2


def _bucket(start, rows):
    # One-token replies report 0 tok/s by construction, and fully cached prompts prefill
    # nothing; neither measures speed, so they only count towards the request total.
    decode = [(row["decode_tokens_per_second"] * row["decode_seconds"], row["decode_seconds"]) for row in rows
              if _number(row.get("decode_tokens_per_second")) and _number(row.get("decode_seconds"))
              and row["decode_tokens_per_second"] > 0 and row["decode_seconds"] > 0]
    prefill = [(row["prefilled_tokens"], row["prefill_seconds"]) for row in rows
               if _number(row.get("prefilled_tokens")) and _number(row.get("prefill_seconds"))
               and row["prefilled_tokens"] > 0 and row["prefill_seconds"] > 0]
    ttft = [row["ttft_seconds"] for row in rows if _number(row.get("ttft_seconds"))]
    return dict(start=start, requests=len(rows), decode=_ratio(decode), prefill=_ratio(prefill),
                ttft=_finite(_median(ttft)) if ttft else None)


def request_series(path, started_at, now, buckets=SERIES_BUCKETS, seconds=SERIES_BUCKET_SECONDS):
    """Per-time-slot request aggregates for the current engine run, oldest slot first.

    Slots are aligned to the clock so bars keep their time while polling. Decode speed is
    weighted by decode time, prefill speed is fresh prompt tokens over prefill time, and
    first-response wait is the median.
    """
    newest = int(now // seconds) * seconds
    first = newest - (buckets - 1) * seconds
    since = max(first, started_at)
    groups = [[] for _ in range(buckets)]
    for row in _completed_requests(Path(path), since):
        if since <= row["time"] < newest + seconds:
            groups[int((row["time"] - first) // seconds)].append(row)
    return dict(bucket_seconds=seconds,
                buckets=[_bucket(first + index * seconds, rows) for index, rows in enumerate(groups)])


class EngineMonitor:
    def __init__(self, log_path, metrics_path=None):
        self.log_path = Path(log_path)
        self.metrics_path = Path(metrics_path) if metrics_path else None
        self.lock = threading.Lock()
        self.log_offset = 0
        self.log_identity = None
        self.proc = None
        self.next_sample = 0
        self.memory = {}
        self.peak = None
        self.started_at = None
        self.series_cache = {}

    def begin(self):
        with self.lock:
            stat = self.log_path.stat() if self.log_path.exists() else None
            self.log_offset = stat.st_size if stat else 0
            self.log_identity = (stat.st_dev, stat.st_ino) if stat else None
            self.proc = None
            self.next_sample = 0
            self.peak = None
            self.memory = {}
            self.started_at = time.time()
            self.series_cache.clear()

    def snapshot(self, proc, chart_seconds=SERIES_BUCKET_SECONDS):
        if type(chart_seconds) is not int or chart_seconds not in SERIES_INTERVALS:
            raise ValueError("차트 시간 단위는 5, 60, 300초만 지원합니다.")
        with self.lock:
            memory = self._memory(proc)
            errors = self._errors()
            return dict(memory=memory, engine_started_at=self.started_at,
                        request_series=self._request_series(proc, chart_seconds), **errors)

    def _request_series(self, proc, seconds):
        if self.metrics_path is None or self.started_at is None or proc is None or proc.poll() is not None:
            return None
        now = time.time()
        try:
            stat = self.metrics_path.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            identity = None
        key = (identity, int(now // seconds), self.started_at)
        cached_key, series = self.series_cache.get(seconds, (None, None))
        if key != cached_key:
            series = request_series(self.metrics_path, self.started_at, now, seconds=seconds)
            self.series_cache[seconds] = (key, series)
        return series

    def _memory(self, proc):
        if proc is None or proc.poll() is not None:
            return dict(state="stopped", rss_bytes=None, sampled_peak_bytes=self.peak)
        now = time.monotonic()
        if self.proc is proc and now < self.next_sample:
            return dict(self.memory)
        if self.proc is not proc:
            self.peak = None
        self.proc, self.next_sample = proc, now + 5
        try:
            result = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,rss="],
                                    capture_output=True, text=True, timeout=2,
                                    env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            if result.returncode:
                raise ValueError("메모리 조회 명령이 실패했습니다.")
            processes = {}
            for line in result.stdout.splitlines():
                pid, parent, rss = map(int, line.split())
                if pid <= 0 or parent < 0 or rss < 0:
                    raise ValueError("메모리 조회 값이 올바르지 않습니다.")
                processes[pid] = (parent, rss)
            if proc.pid not in processes or proc.poll() is not None:
                raise ValueError("샘플링 중 엔진이 종료되어 메모리를 확인하지 못했습니다.")
            owned = {proc.pid}
            while True:
                children = {pid for pid, (parent, _) in processes.items() if parent in owned}
                if children <= owned:
                    break
                owned.update(children)
            rss = sum(processes[pid][1] for pid in owned) * 1024
            self.peak = max(self.peak or 0, rss)
            self.memory = dict(state="measured", rss_bytes=rss, sampled_peak_bytes=self.peak,
                               process_count=len(owned), sampled_at=time.time())
        except (OSError, ValueError, subprocess.SubprocessError):
            self.memory = dict(state="unavailable", rss_bytes=None, sampled_peak_bytes=self.peak,
                               error="엔진 메모리를 읽지 못했습니다. 다음 샘플링 때 다시 확인합니다.")
        return dict(self.memory)

    def _errors(self):
        try:
            with self.log_path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                same_log = self.log_identity == (stat.st_dev, stat.st_ino)
                start = self.log_offset if same_log and stat.st_size >= self.log_offset else 0
                offset = max(start, stat.st_size - 65536)
                stream.seek(offset)
                data = stream.read(65536)
            if offset > start:
                data = data.partition(b"\n")[2]
        except FileNotFoundError:
            return dict(errors=[], error_read_failure=None)
        except OSError:
            return dict(errors=[], error_read_failure="엔진 진단 로그를 읽지 못했습니다.")
        counts = {}
        for line in data.decode("utf-8", errors="replace").splitlines():
            lower = line.lower()
            if not re.search(r"\b(?:error|exception|traceback|fatal)\b|\b[a-z]+error\b|out of memory|failed to allocate", lower):
                continue
            if any(word in lower for word in ("out of memory", "failed to allocate", "memoryerror")):
                category = "메모리 할당 오류"
            elif any(word in lower for word in ("safetensors", "checkpoint", "model", "tokenizer")):
                category = "모델 로드·파일 관련 오류"
            elif any(word in lower for word in ("timeout", "timed out")):
                category = "처리 시간 초과"
            elif any(word in lower for word in ("address already in use", "bind", "connection")):
                category = "포트·연결 관련 오류"
            else:
                category = "엔진 실행 오류"
            counts[category] = counts.get(category, 0) + 1
        return dict(errors=[dict(category=category, lines=count) for category, count in counts.items()],
                    error_read_failure=None)
