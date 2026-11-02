import collections
import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from .connections import ConfigError, Connections
from .monitor import EngineMonitor, SERIES_BUCKET_SECONDS, SERIES_INTERVALS
from .hardware import (CHUNKS, DETECTOR, REC_DEFAULT_WORKERS, defaults as hardware_defaults,
                       hung_dominates, measurement_environment,
                       macos_requirement, requirements, tuning_plan, worker_candidates,
                       worker_options, worker_validity)

ROOT = Path(__file__).resolve().parents[1]
REC_SHA = "76874faf4ba16b1907ebef0d6a92147a1f81b95df92d805ae5b98019df265bed"
REC_URL = "https://github.com/recursive-oss/rec/releases/download/v0.1.1/rec-darwin-arm64"
DEFAULTS = dict(context=65536, port=11540, workers=[36, 32, 32], chunk=2048,
                disk_cache=False, cache_gb=8, temperature=1.0, top_p=0.95,
                top_k=20, presence_penalty=0.0, output_limit=8192)
CONTEXT_CHOICES = (32768, 65536, 131072, 262144)
CACHE_GB_CHOICES = (8, 16, 32, 64, 128, 192, 256, 384, 512)
TUNING_MARGIN = 0.03
MODEL_FILES = frozenset({"chat_template.jinja", "config.json", "generation_config.json",
                         "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json",
                         *[f"model-{i:05d}.safetensors" for i in range(5)]})


class Problem(Exception):
    pass


class Cancelled(Problem):
    pass


class EngineStartError(Problem):
    """A launched engine failed before readiness, after passing preflight checks."""


class EngineStartTimeout(EngineStartError):
    """A launched engine did not become ready within the startup deadline."""


def atomic(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as out:
        out.write(content)
    os.chmod(temp, mode)
    os.replace(temp, path)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def clone_file(source, destination):
    # clonefile fails on unsupported volumes instead of silently copying 21 GiB.
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    clone = library.clonefile
    clone.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 1) != 0:  # CLONE_NOFOLLOW
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(source))


def upper_median(values):
    return sorted(values)[len(values) // 2]


def candidates(hardware):
    return [dict(workers=w, chunk=c) for w in worker_candidates(hardware) for c in CHUNKS]


def select_best(baseline, results):
    valid = [r for r in results if r.get("score") is not None]
    if baseline is None or not valid:
        return None
    best = max(valid, key=lambda r: r["score"])
    return best if best["score"] > baseline * (1 + TUNING_MARGIN) else None


def calibration_cache_gb(free_bytes):
    if type(free_bytes) is not int or free_bytes < 0:
        raise Problem("사용 가능한 디스크 용량이 올바르지 않습니다.")
    budget = free_bytes // 5 // (2**30)
    return max((size for size in CACHE_GB_CHOICES if size <= budget), default=CACHE_GB_CHOICES[0])


def validate(value, hardware=None, check_workers=True):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise Problem("설정 항목이 올바르지 않습니다.")
    ranges = dict(context=(4096, 262144, int), port=(1024, 65535, int),
                  chunk=(1024, 4096, int), cache_gb=(1, 512, int),
                  temperature=(0, 2, (int, float)), top_p=(0.01, 1, (int, float)),
                  top_k=(0, 150144, int), presence_penalty=(-2, 2, (int, float)),
                  output_limit=(1, 32768, int))
    for key, (low, high, kind) in ranges.items():
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, kind) or not math.isfinite(v) or not low <= v <= high:
            raise Problem(f"설정 범위 오류: {key}")
    if value["port"] ==  web_port():
        raise Problem("추론 포트와 웹 포트가 같을 수 없습니다.")
    if type(value["disk_cache"]) is not bool or value["chunk"] not in (1024, 2048, 4096):
        raise Problem("캐시 또는 chunk 설정 오류")
    if (not isinstance(value["workers"], list) or len(value["workers"]) != 3
            or any(type(v) is not int or not 1 <= v <= 2048 for v in value["workers"])):
        raise Problem("workers는 양의 정수 3개여야 합니다.")
    if check_workers:
        hardware = hardware if hardware is not None else DETECTOR.snapshot()
        if value["workers"] not in worker_options(hardware):
            raise Problem("감지된 하드웨어에서 허용하지 않거나 검증할 수 없는 workers입니다.")
    if value["output_limit"] > value["context"]:
        raise Problem("출력 한도는 전체 문맥 한도 이하여야 합니다.")
    return value


def web_port():
    return int(os.environ.get("FLINT_WEB_PORT", "11539"))


def sanitized_env():
    # Do not let inherited model overrides, Python paths or API credentials affect the engine.
    return {k: v for k, v in os.environ.items()
            if k in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")}


class Manager:
    def __init__(self, home=None, detector=None):
        self.detector = detector if detector is not None else DETECTOR
        self.home = Path(home or Path.home() / ".local/share/flint-local")
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.home, 0o700)
        self.lock = threading.Lock()
        self.tuning_lock = threading.RLock()
        self.restoring = False
        self.cancel = threading.Event()
        self.proc = None
        self.monitor = EngineMonitor(self.home / "engine.log", self.home / "metrics.jsonl")
        self.task_proc = None
        self.active_settings = None
        self.ready = False
        self.closing = False
        self.benchmark_token = None
        self.job = dict(state="idle", action=None, message="대기 중")
        self.logs = collections.deque(maxlen=400)
        self.config_file = self.home / "settings.json"
        self.config = (validate(json.loads(self.config_file.read_text()), check_workers=False)
                       if self.config_file.exists() else {**DEFAULTS, **hardware_defaults(self.detector.snapshot())})
        self.key_file = self.home / "rec/config/api-key"
        if not self.key_file.exists():
            atomic(self.key_file, secrets.token_hex(32) + "\n")
        self.tuning = self.read_json("tuning.json", {})
        self.tuning_history = {}
        for path in (self.home / "tuning-history").glob("*.json"):
            report = json.loads(path.read_text())
            self.tuning_history[path.stem] = self.history_summary(report)
        self.log("Flint Kit 제어 서버가 준비되었습니다.")

    def read_json(self, name, default):
        p = self.home / name
        return json.loads(p.read_text()) if p.exists() else default

    @staticmethod
    def history_summary(report):
        environment = report.get("environment")
        context = report.get("context", report.get("settings_before", {}).get("context"))
        key = hashlib.sha256(json.dumps(dict(environment=environment, context=context),
                                       sort_keys=True).encode()).hexdigest()
        return dict(tuning_id=report["tuning_id"], key=key, context=context, environment=environment,
                    state=report.get("state"), current_only=report.get("current_only", False),
                    finished_at=report.get("finished_at"), applied=report.get("applied", False),
                    adopted=report.get("adopted", False), recovered=report.get("recovered", False))

    def archive_tuning(self, report):
        stored = copy.deepcopy(report)
        if not stored.get("tuning_id"):
            stored["tuning_id"] = str(uuid.uuid5(uuid.NAMESPACE_OID, json.dumps(stored, sort_keys=True)))
        identifier = str(uuid.UUID(stored["tuning_id"]))
        stored["tuning_id"] = identifier
        summary = self.history_summary(stored)
        atomic(self.home / "tuning-history" / (identifier + ".json"), json.dumps(stored, indent=2))
        with self.tuning_lock:
            self.tuning_history[identifier] = summary

    def historical_tuning(self, identifier):
        try:
            if str(uuid.UUID(identifier)) != identifier:
                raise ValueError("noncanonical identifier")
        except (ValueError, TypeError, AttributeError):
            raise Problem("올바른 튜닝 기록 ID가 필요합니다.") from None
        with self.tuning_lock:
            if identifier not in self.tuning_history:
                raise Problem("튜닝 기록을 찾을 수 없습니다.")
        path = self.home / "tuning-history" / (identifier + ".json")
        if path.is_symlink():
            raise Problem("튜닝 기록이 심볼릭 링크입니다. 읽지 않았습니다.")
        return json.loads(path.read_text())

    def log(self, message):
        key = self.key_file.read_text().strip() if self.key_file.exists() else ""
        if key:
            message = str(message).replace(key, "[REDACTED]")
        row = dict(time=time.time(), message=str(message)[:1000])
        self.logs.append(row)
        with (self.home / "activity.jsonl").open("a") as out:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(self, args, timeout=3600, env=None):
        self.log("실행: " + " ".join(str(a) for a in args))
        p = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=env or self.env(), start_new_session=True)
        self.task_proc = p
        started = time.monotonic()
        output = collections.deque(maxlen=50)
        # A reader thread prevents a quiet subprocess from blocking cancellation.
        def read():
            for line in p.stdout:
                output.append(line.strip())
                self.log(line.strip())
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            while p.poll() is None:
                if self.cancel.wait(0.2):
                    raise Cancelled("작업 취소됨")
                if time.monotonic() - started > timeout:
                    raise Problem("작업 제한 시간 초과")
            reader.join(2)
            if p.returncode:
                raise Problem(f"명령 실패 (exit {p.returncode}): " + "\n".join(output)[-1800:])
        finally:
            if p.poll() is None:
                self.terminate(p)
            self.task_proc = None

    @staticmethod
    def terminate(p):
        # Only Popen handles created by this controller are signalled; no PID files.
        if p.poll() is None:
            import signal
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(25)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(10)

    def env(self, config=None):
        c = config or self.config
        return {**sanitized_env(), "XDG_DATA_HOME": str(self.home), "FLINT_HOME": str(self.home),
                "REC_HOST": "127.0.0.1", "REC_EXP_WORKERS": ",".join(map(str, c["workers"])),
                "REC_EXP_CHUNK": str(c["chunk"]), "REC_DISK_CACHE": str(self.home / "kv-cache") if c["disk_cache"] else "0",
                "REC_DISK_CACHE_GB": str(c["cache_gb"]), "REC_METRICS": str(self.home / "metrics.jsonl"),
                "FLINT_BENCHMARK_TOKEN": self.benchmark_token or "",
                **{f"REC_{k.upper()}": str(c[k]) for k in
                   ("temperature", "top_p", "top_k", "presence_penalty", "output_limit")}}

    def submit(self, action, fn):
        if not self.lock.acquire(blocking=False):
            raise Problem("다른 작업이 진행 중입니다. 완료 또는 취소 후 다시 시도하세요.")
        self.cancel.clear()
        self.job = dict(state="running", action=action, message="시작 중")
        def work():
            try:
                fn()
                self.job.update(state="done", message="완료")
                self.log(action + ": 완료")
            except Cancelled as error:
                self.job.update(state="cancelled", message=str(error))
                self.log(str(error))
            except Exception as error:
                self.job.update(state="error", message=str(error))
                self.log(action + ": " + str(error))
            finally:
                self.lock.release()
        threading.Thread(target=work, daemon=True).start()

    def install(self):
        self.require_stopped()
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise Problem("이 엔진은 Apple Silicon macOS 전용입니다.")
        self.require_macos()
        dest = self.home / "bin/rec"
        dest.parent.mkdir(exist_ok=True, mode=0o700)
        if not dest.exists() or digest(dest) != REC_SHA:
            temp = dest.with_suffix(".download")
            self.log("공식 rec v0.1.1 다운로드: 고정 SHA-256 검증 후에만 실행합니다.")
            self.run(["/usr/bin/curl", "--proto", "=https", "--proto-redir", "=https", "-fL",
                      "--connect-timeout", "20", "--max-time", "1800", "-o", temp, REC_URL])
            if digest(temp) != REC_SHA:
                temp.unlink()
                raise Problem("rec SHA-256 불일치: 실행하지 않았습니다.")
            temp.chmod(0o700)
            os.replace(temp, dest)
        imported = self.reuse_models()
        import sys
        self.run([sys.executable, ROOT / "vendor/rec-overlay/install.py"], timeout=1800)
        if imported:
            self.model_command("doctor")

    def reuse_models(self):
        self.require_stopped()
        self.require_macos()
        target = self.home / "rec/models"
        if target.is_symlink():
            raise Problem("모델 폴더가 심볼릭 링크입니다. 가져오지 않았습니다.")
        if target.exists() and any(target.iterdir()):
            self.log("기존 Flint Kit 모델/다운로드를 보존합니다. rec 모델을 덮어 가져오지 않습니다.")
            return False
        source = Path.home() / ".local/share/rec/models"
        if not source.exists():
            return False
        if source.is_symlink() or source.parent.is_symlink():
            raise Problem("기존 rec 모델 경로가 심볼릭 링크입니다. 자동으로 가져오지 않습니다.")
        manifests = list(source.glob("*/flint-int4.json"))
        if not manifests:
            return False
        if len(manifests) != 1:
            raise Problem("기존 rec 모델이 여러 개입니다. 자동으로 선택하지 않았습니다.")
        manifest = manifests[0]
        model = manifest.parent
        files = [model / name for name in sorted(MODEL_FILES | {"flint-int4.json"})]
        if model.is_symlink() or any(p.is_symlink() or not p.is_file() for p in files):
            raise Problem("기존 rec 모델이 불완전하거나 심볼릭 링크를 포함합니다. 가져오지 않았습니다.")
        try:
            if manifest.stat().st_size > 1024 * 1024:
                raise ValueError("manifest too large")
            metadata = json.loads(manifest.read_text())
            if set(metadata["checkpoint_files"]) != MODEL_FILES:
                raise ValueError("unexpected checkpoint files")
        except (ValueError, KeyError, TypeError) as error:
            raise Problem("기존 rec 모델의 메타데이터가 올바르지 않습니다. 가져오지 않았습니다.") from error
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log("기존 rec 모델을 APFS 복제로 가져옵니다. 원본과 검증 기록은 변경하지 않습니다.")
        with tempfile.TemporaryDirectory(prefix=".model-import-", dir=target) as staging:
            staged = Path(staging) / model.name
            staged.mkdir(mode=0o700)
            try:
                for path in files:
                    if self.cancel.is_set():
                        raise Cancelled("모델 가져오기 취소됨")
                    clone_file(path, staged / path.name)
                if self.cancel.is_set():
                    raise Cancelled("모델 가져오기 취소됨")
                destination = target / model.name
                if os.path.lexists(destination):
                    raise Problem("모델 가져오기 중 대상이 생성되었습니다. 기존 파일을 보존합니다.")
                staged.rename(destination)
            except OSError as error:
                raise Problem("APFS 모델 복제 실패: " + str(error)
                              + " · 일반 복사나 다운로드로 자동 전환하지 않았습니다.") from error
        self.log("모델 복제 완료. 사본은 rec doctor로 새로 검증해야 사용할 수 있습니다.")
        return True

    def model_command(self, command):
        self.require_stopped()
        self.require_macos()
        rec = self.home / "bin/rec"
        if not rec.is_file() or digest(rec) != REC_SHA:
            raise Problem("먼저 검증된 엔진을 설치하세요.")
        if command == "pull" and self.reuse_models():
            command = "doctor"
        self.run([rec, command], timeout=21600)
        if not self.models_verified():
            raise Problem("검증된 모델 메타데이터가 없습니다. 모델 다운로드/검증 로그를 확인하세요.")

    def models_verified(self):
        manifests = list((self.home / "rec/models").glob("*/flint-int4.json"))
        if len(manifests) != 1:
            return False
        manifest_path = manifests[0]
        try:
            manifest = json.loads(manifest_path.read_text())
            checksums = manifest["checkpoint_files"]
            verified = json.loads((manifest_path.parent / "verified.json").read_text())
            if set(checksums) != MODEL_FILES:
                return False
            for name, checksum in {**checksums, "flint-int4.json": digest(manifest_path)}.items():
                path = manifest_path.parent / name
                if path.is_symlink() or not path.is_file():
                    return False
                stat = path.stat()
                record = verified.get(name, {})
                expected = dict(sha256=checksum, size=stat.st_size, device=stat.st_dev,
                                inode=stat.st_ino, mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)
                if any(record.get(k) != v for k, v in expected.items()):
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def running(self):
        if self.proc is not None and self.proc.poll() is not None:
            self.ready = False
        return self.proc is not None and self.proc.poll() is None

    def require_stopped(self):
        if self.running():
            raise Problem("먼저 Flint Kit 소유 서버를 중지하세요.")

    def require_macos(self):
        check = macos_requirement(self.detector.snapshot())
        if check["passed"] is not True:
            raise Problem(check["name"] + ": " + check["reason"])

    def external_engine(self):
        p = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5)
        if p.returncode:
            raise Problem("실행 중인 엔진을 확인할 수 없습니다.")
        own = self.proc.pid if self.running() else None
        for line in p.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and int(parts[0]) != own and (
                    "rec_mlx.cli serve" in parts[1] or "rec serve" in parts[1] and not " --help" in parts[1]):
                return True
        return False

    def request(self, route, body=None, timeout=10):
        c = self.active_settings or self.config
        req = urllib.request.Request(f"http://127.0.0.1:{c['port']}{route}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.key_file.read_text().strip(),
                                              "Content-Type": "application/json",
                                              **({"X-Flint-Benchmark": self.benchmark_token}
                                                 if self.benchmark_token else {})})
        # Never route a loopback request through inherited proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if body is None:
            with opener.open(req, timeout=timeout) as response:
                return json.load(response)
        done = threading.Event()
        result = []
        errors = []
        def send():
            try:
                with opener.open(req, timeout=timeout) as response:
                    result.append(json.load(response))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()
        threading.Thread(target=send, daemon=True).start()
        while not done.wait(0.1):
            if self.cancel.is_set():
                raise Cancelled("추론 요청 취소됨")
        if errors:
            raise errors[0]
        return result[0]

    def start(self, config=None):
        self.require_stopped()
        if self.closing:
            raise Cancelled("제어 서버 종료 중")
        self.require_macos()
        c = dict(config or self.config)
        validate(c, check_workers=False)
        validity = worker_validity(c["workers"], self.detector.snapshot())
        if validity["state"] == "invalid":
            raise Problem(validity["reason"])
        if validity["reason"]:
            self.log("경고: " + validity["reason"])
        if self.external_engine():
            raise Problem("다른 추론 엔진이 실행 중입니다. 중복 모델 로드를 막기 위해 시작하지 않았습니다.")
        if not (self.home / "rec-tuned/manifest.json").is_file():
            raise Problem("엔진 설치가 필요합니다.")
        if not any((self.home / "rec/models").glob("*/config.json")):
            raise Problem("모델 가져오기 또는 다운로드가 필요합니다.")
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", c["port"]))
            except OSError:
                raise Problem("추론 포트가 사용 중입니다. 다른 프로세스를 중지하지 않았습니다.") from None
        if not self.models_verified():
            raise Problem("모델 파일 또는 검증 기록이 변경되었습니다. 먼저 무결성 검증을 실행하세요. 자동 다운로드하지 않습니다.")
        rec = self.home / "bin/rec"
        if digest(rec) != REC_SHA:
            raise Problem("실행 파일 SHA-256 불일치")
        args = [str(rec), "serve", "--manifest", str(self.home / "rec-tuned/manifest.json"),
                "--runtime-dir", str(self.home / "rec-tuned/rt"),
                "--port", str(c["port"]), "--context", str(c["context"])]
        log_path = self.home / "engine.log"
        if log_path.exists() and log_path.stat().st_size > 5_000_000:
            os.replace(log_path, self.home / "engine.previous.log")
        self.monitor.begin()
        with log_path.open("a") as out:
            # rec serve can otherwise repair missing files via HTTP. Setup actions, not
            # lifecycle actions, are the only place where downloads are permitted.
            env = {**self.env(c), "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                   "NO_PROXY": "127.0.0.1,localhost"}
            self.proc = subprocess.Popen(args, env=env, stdout=out, stderr=out, start_new_session=True)
        self.active_settings = c
        self.ready = False
        self.log("추론 서버 시작 중 (모델 로드).")
        end = time.monotonic() + 300
        try:
            while time.monotonic() < end:
                if self.cancel.wait(0.5):
                    raise Cancelled("서버 시작 취소됨")
                if not self.running():
                    raise EngineStartError("엔진이 시작 중 종료되었습니다. 비공개 engine.log를 확인하세요.")
                try:
                    health = self.request("/health", timeout=1)
                    if health:
                        self.ready = True
                        self.log("추론 서버 준비 완료.")
                        return
                except (OSError, ValueError):
                    continue
            raise EngineStartTimeout("모델 시작 제한 시간 초과")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.proc is not None:
            self.terminate(self.proc)
        self.proc = None
        self.ready = False
        self.active_settings = None
        self.log("Flint Kit 소유 추론 서버 중지됨.")

    def save(self, data):
        self.require_stopped()
        hardware = self.detector.snapshot()
        unchanged_unverified = (isinstance(data, dict) and data.get("workers") == self.config["workers"]
                                and worker_validity(self.config["workers"], hardware)["state"] == "unverified")
        value = dict(validate(data, hardware, check_workers=not unchanged_unverified))
        atomic(self.config_file, json.dumps(value, indent=2))
        self.config = value

    def clear_cache(self):
        self.require_stopped()
        path = self.home / "kv-cache"
        if path.is_symlink():
            raise Problem("캐시가 심볼릭 링크입니다. 삭제하지 않았습니다.")
        if path.exists():
            shutil.rmtree(path)
        self.log("Flint Kit SSD 프롬프트 캐시만 삭제했습니다.")

    def metrics(self):
        p = self.home / "metrics.jsonl"
        if not p.exists():
            return []
        with p.open("rb") as f:
            f.seek(max(0, p.stat().st_size - 100_000))
            lines = f.read().splitlines()[-60:]
        result = []
        for line in lines:
            try:
                row = json.loads(line)
                result.append({k: v for k, v in row.items()
                               if type(v) in (int, float) and abs(v) <= 1e308
                               or k == "status" and v in ("complete", "cancelled", "error", "cancelled_or_failed")})
            except (ValueError, AttributeError):
                continue
        return result

    def status(self, chart_seconds=SERIES_BUCKET_SECONDS):
        with self.tuning_lock:
            tuning = copy.deepcopy(self.tuning)
            history = sorted(copy.deepcopy(list(self.tuning_history.values())),
                             key=lambda row: row["finished_at"] or 0, reverse=True)[:100]
        cache = self.home / "kv-cache"
        free_bytes = shutil.disk_usage(self.home).free
        hardware = self.detector.snapshot()
        models = any((self.home / "rec/models").glob("*/config.json"))
        environment = measurement_environment(hardware)
        previous_environment = tuning.get("environment")
        comparison = ("missing" if previous_environment is None else
                      "unknown" if any(v is None for v in environment.values()) or
                      any(previous_environment.get(k) is None for k in environment) else
                      "same" if previous_environment == environment else "different")
        proc = self.proc
        exit_code = proc.poll() if proc is not None else None
        running = proc is not None and exit_code is None
        return dict(settings=self.config, active_settings=self.active_settings,
                    capabilities=dict(tune_apply=True, tune_reliability=True, tune_recovery=True,
                                      tuning_history=True, client_connections=True, engine_monitor=True,
                                      chart_intervals=True, chart_interval_seconds=SERIES_INTERVALS),
                    hardware={**hardware, "memory_gib": environment["memory_gib"],
                              "free_bytes": free_bytes, "checks": requirements(hardware, free_bytes, models),
                              "macos_check": macos_requirement(hardware),
                              "defaults": hardware_defaults(hardware),
                              "worker_options": worker_options(hardware),
                              "workers_status": worker_validity(self.config["workers"], hardware)},
                    tuning_plan=tuning_plan(hardware, self.config["workers"]),
                    measurement_environment=environment, tuning_environment_status=comparison,
                    engine=dict(running=running, ready=self.ready and running, exit_code=exit_code,
                                installed=(self.home / "rec-tuned/manifest.json").is_file(),
                                models=models,
                                verified=self.models_verified(),
                                pid=proc.pid if running else None),
                    job=self.job.copy(), logs=list(self.logs)[-100:], metrics=self.metrics(),
                    monitor=self.monitor.snapshot(proc, chart_seconds),
                    tuning=tuning, tuning_history=history,
                    candidates=candidates(hardware), integrations=self.integration_status(),
                    cache_bytes=sum(p.stat().st_size for p in cache.rglob("*") if p.is_file()) if cache.exists() else 0,
                    free_bytes=free_bytes,
                    calibration=dict(context_choices=CONTEXT_CHOICES, context_policy="preserve",
                                     cache_gb_choices=CACHE_GB_CHOICES,
                                     cache_gb=calibration_cache_gb(free_bytes)),
                    data_dir=str(self.home), api_base=f"http://127.0.0.1:{self.config['port']}/v1")

    @staticmethod
    def filler(nominal, identifier):
        rows = [f"Line {i + 1} of calibration text {identifier[:8]} with a few ordinary words."
                for i in range(max(1, nominal // 15))]
        return f"Calibration {identifier}\n" + "\n".join(rows)

    def probe(self, prompt, limit, timeout, temperature=0.6, check=False):
        payload = dict(model="flint", messages=[{"role": "user", "content": prompt}],
                       temperature=temperature, max_tokens=limit, disk_cache=False)
        if check:
            payload["seed"] = 0
        metric_path = self.home / "metrics.jsonl"
        offset = metric_path.stat().st_size if metric_path.exists() else 0
        response = self.request("/v1/chat/completions", payload, timeout=timeout)
        usage = response.get("usage", {})
        choices = response.get("choices", [])
        message = choices[0].get("message") if choices else None
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or any(k in message and not isinstance(message[k], str) for k in ("content", "reasoning"))
                or not isinstance(usage.get("prompt_tokens"), int)
                or not isinstance(usage.get("completion_tokens"), int) or usage["completion_tokens"] < 1):
            raise Problem("잘못된 벤치마크 응답입니다. 성공으로 간주하지 않습니다.")
        if check:
            if not any(message.get(k) for k in ("content", "reasoning", "tool_calls")):
                raise Problem("greedy 검증 응답에 검증 가능한 출력이 없습니다.")
            return dict(answer_sha256=hashlib.sha256(
                json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())
        end = time.monotonic() + 3
        records = []
        while time.monotonic() < end:
            if metric_path.exists():
                with metric_path.open("rb") as file:
                    file.seek(offset)
                    records = [json.loads(line) for line in file.read().splitlines() if line.strip()]
                if records:
                    break
            time.sleep(0.05)
        if len(records) != 1 or records[0].get("status") != "complete":
            raise Problem("요청에 대응하는 단일 완료 metrics를 찾지 못했습니다.")
        row = records[0]
        if row.get("prompt_tokens") != usage["prompt_tokens"]:
            raise Problem("metrics와 요청의 prompt_tokens가 일치하지 않습니다.")
        if limit > 1 and usage["completion_tokens"] < 64:
            raise Problem("측정에 불충분한 출력: 64토큰 미만")
        score = row.get("decode_tokens_per_second")
        if limit > 1 and (not isinstance(score, (int, float)) or not math.isfinite(score) or score <= 0):
            raise Problem("유효한 decode 속도가 없습니다.")
        return {k: v for k, v in row.items() if type(v) in (int, float)}

    def tune(self, current_only=False):
        try:
            return self._tune(current_only)
        finally:
            with self.tuning_lock:
                self.restoring = False
                self.benchmark_token = None
                self.cancel.clear()

    def _tune(self, current_only=False):
        hardware = self.detector.snapshot()
        plan = tuning_plan(hardware, self.config["workers"])
        if not plan["allowed"]:
            raise Problem("튜닝 불가: " + plan["reason"])
        if self.tuning.get("state") and self.tuning["state"] != "running":
            self.archive_tuning(self.tuning)
        original = dict(self.config)
        reference_candidate = {key: original[key] for key in ("workers", "chunk")}
        was_running = self.running()
        report = dict(state="running", policy="workers: mean of 3 short/long upper medians of decode tok/s; chunk: median of 3 fresh prefill tok/s; >3%; baseline: 3 identical greedy message SHA-256 checks; startup: retry once only after launched-engine exit/readiness timeout; full tune recovery: rec 36/32/32 only on baseline startup failure; prune: componentwise greater candidates after failed startup with readiness timeout; final: remeasure actual baseline and proposed combinations, changed metrics >3% and neither metric drops >3%; recovery is not an improvement over an unmeasured original; context: preserve configured value; full tune cache: largest GiB choice <= free/5, fallback 8",
                           results=[], adopted=False, current_only=current_only,
                           tuning_id=str(uuid.uuid4()), applied=False, pending=False,
                           baseline_check=dict(state="pending", required=3, completed=0),
                           verification=dict(state="not_run", reason=""),
                           fallback=dict(state="not_used"), recovered=False,
                           measurement_baseline=reference_candidate,
                           context=original["context"], context_policy="preserve",
                           environment=measurement_environment(hardware),
                           worker_candidates=plan["workers"],
                           settings_before=original,
                           measurement_context=max(original["context"], 65536))
        chosen = original
        failure = None
        started = time.time()
        completed_durations = []
        total = 1 if current_only else len(plan["workers"]) + len(plan["chunks"]) + 2
        def progress(phase, detail, candidate=None):
            with self.tuning_lock:
                report["progress"] = dict(
                    phase=phase, detail=detail, candidate=candidate,
                    trial=min(len(report["results"]) + 1, total), total=total,
                    completed=len(report["results"]), started_at=started, updated_at=time.time(),
                    eta_seconds=(sum(completed_durations) / len(completed_durations)
                                 * (total - len(report["results"]))) if completed_durations else None)
                self.tuning = copy.deepcopy(report)
        progress("workers", "측정 준비")
        try:
            self.stop()
            self.benchmark_token = secrets.token_urlsafe(32)
            identifier = str(uuid.uuid4())
            short = "Count from 1 upward, one number per line."
            long = self.filler(32000, identifier) + "\n" + short
            check_prompt = "Name three prime numbers and add them. Show the sum."
            trials = [dict(workers=original["workers"], chunk=original["chunk"])]
            if not current_only:
                trials.extend(dict(workers=w, chunk=original["chunk"])
                              for w in plan["workers"] if w != original["workers"])
            baseline = None
            expected = None
            def measure(trial, phase, checks=1):
                nonlocal expected
                if self.cancel.is_set():
                    raise Cancelled("튜닝 취소됨")
                c = {**original, **trial, "disk_cache": False, "context": report["measurement_context"]}
                self.job["message"] = f"{phase} 후보: {trial}"
                trial_started = time.monotonic()
                rows = []
                error = None
                prefill = []
                answer_hash = None
                answer_hashes = []
                start_errors = []
                start_attempts = 0
                failure_kind = None
                startup_timed_out = False
                establishing_baseline = expected is None
                try:
                    for attempt in (1, 2):
                        if self.cancel.is_set() or self.closing:
                            raise Cancelled("튜닝 시작 취소됨")
                        start_attempts = attempt
                        progress(phase, "엔진 시작" if attempt == 1 else "엔진 시작 재시도 2/2", trial)
                        try:
                            self.start(c)
                            break
                        except EngineStartError as start_error:
                            start_errors.append(str(start_error))
                            startup_timed_out |= isinstance(start_error, EngineStartTimeout)
                            self.log(f"튜닝 {phase} 엔진 시작 실패 {attempt}/2: {start_error}")
                            if attempt == 2:
                                failure_kind = "startup_timeout" if startup_timed_out else "startup_exit"
                                raise
                            self.stop()
                            progress(phase, "엔진 시작 실패 · 1초 후 한 번 재시도", trial)
                            if self.cancel.wait(1):
                                raise Cancelled("엔진 시작 재시도 취소됨")
                    for check_index in range(checks):
                        if self.cancel.is_set():
                            raise Cancelled("출력 검증 취소됨")
                        detail = ("기준 응답 재현성" if establishing_baseline else "출력 재현성")
                        progress(phase, f"{detail} {check_index + 1}/{checks}" if checks > 1 else "출력 검증", trial)
                        answer = self.probe(check_prompt, 96, 90, temperature=0, check=True)
                        answer_hash = answer["answer_sha256"]
                        answer_hashes.append(answer_hash)
                        if establishing_baseline:
                            report["baseline_check"]["completed"] = len(answer_hashes)
                            if answer_hash != answer_hashes[0]:
                                raise Problem("현재 설정의 기준 응답이 반복 실행에서 달라졌습니다. 재현성을 확인할 수 없어 채택하지 않습니다.")
                        elif answer_hash != expected:
                            raise Problem("별도 greedy 검증 응답 불일치: 후보를 채택하지 않습니다.")
                    if establishing_baseline:
                        expected = answer_hash
                        report["baseline_check"]["state"] = "passed"
                    if phase != "chunk":
                        progress(phase, "워밍업", trial)
                        self.probe(long, 1, 240)
                        for group, prompt, timeout in (("short", short, 60), ("long", long, 240)):
                            for round_index in range(3):
                                if self.cancel.is_set():
                                    raise Cancelled("튜닝 취소됨")
                                progress(phase, f"{'짧은' if group == 'short' else '긴'} 생성 {round_index + 1}/3", trial)
                                row = self.probe(prompt, 256, timeout)
                                rows.append(dict(group=group, **row))
                    for round_index in range(3):
                        if self.cancel.is_set():
                            raise Cancelled("튜닝 취소됨")
                        progress(phase, f"읽기 {round_index + 1}/3", trial)
                        row = self.probe(self.filler(8000, str(uuid.uuid4())) + "\nReply with OK.", 1, 120)
                        tokens, seconds = row.get("prefilled_tokens"), row.get("prefill_seconds")
                        if (not isinstance(tokens, (int, float)) or not isinstance(seconds, (int, float))
                                or not math.isfinite(tokens) or not math.isfinite(seconds)
                                or tokens <= 0 or seconds <= 0):
                            raise Problem("유효한 fresh prefill 측정값이 없습니다.")
                        prefill_rate = tokens / seconds
                        if not math.isfinite(prefill_rate) or prefill_rate <= 0:
                            raise Problem("유효한 fresh prefill 속도가 없습니다.")
                        prefill.append(dict(score=prefill_rate, **row))
                except Cancelled:
                    raise
                except (Problem, OSError, ValueError, KeyError) as e:
                    error = str(e)
                finally:
                    if establishing_baseline and report["baseline_check"]["state"] != "passed":
                        report["baseline_check"].update(state="failed", error=error or "기준 응답 검증이 완료되지 않았습니다.")
                    self.stop()
                prefill_score = upper_median([r["score"] for r in prefill]) if len(prefill) == 3 else None
                decode_score = (sum(upper_median([r["decode_tokens_per_second"] for r in rows if r["group"] == group])
                                     for group in ("short", "long")) / 2) if len(rows) == 6 else None
                scores = [prefill_score] if phase == "chunk" else [decode_score, prefill_score]
                if error is None and any(v is None or not math.isfinite(v) or v <= 0 for v in scores):
                    error = "유효한 집계 속도가 없습니다. 후보를 채택하지 않습니다."
                if decode_score is not None and not math.isfinite(decode_score):
                    decode_score = None
                score = (prefill_score if phase == "chunk" else decode_score) if error is None else None
                result = dict(phase=phase, candidate=trial, score=score, rounds=rows,
                              prefill_rounds=prefill, prefill_score=prefill_score, decode_score=decode_score,
                              answer_sha256=answer_hash, answer_hashes=answer_hashes,
                              start_attempts=start_attempts, start_errors=start_errors,
                              failure_kind=failure_kind, error=error)
                with self.tuning_lock:
                    report["results"].append(result)
                completed_durations.append(time.monotonic() - trial_started)
                progress(phase, "후보 측정 완료", trial)
                atomic(self.home / "tuning.json", json.dumps(report, indent=2))
                return result
            def explore(trial, phase):
                if self.cancel.is_set():
                    raise Cancelled("튜닝 취소됨")
                blocker = next((r for r in report["results"]
                                if r["failure_kind"] == "startup_timeout"
                                and (r["phase"] == phase or phase == "workers" and r["phase"] == "fallback_baseline")
                                and hung_dominates(r["candidate"], trial, phase)), None)
                if blocker:
                    reason = f"{blocker['candidate']} 준비 시간 초과 후 더 큰 후보를 건너뜁니다."
                    result = dict(phase=phase, candidate=trial, score=None, error=None, skipped=True,
                                  skip_reason=reason, skipped_after=blocker["candidate"],
                                  start_attempts=0, start_errors=[], failure_kind=None)
                    report["results"].append(result)
                    self.log(reason)
                    progress(phase, "멈춘 값보다 큰 후보 건너뜀", trial)
                    atomic(self.home / "tuning.json", json.dumps(report, indent=2))
                    return result
                return measure(trial, phase)

            result = measure(trials[0], "workers", checks=3)
            remaining = trials[1:]
            if (result["score"] is None and not current_only
                    and result["failure_kind"] in ("startup_exit", "startup_timeout")
                    and original["workers"] != list(REC_DEFAULT_WORKERS)):
                reference_candidate = dict(workers=list(REC_DEFAULT_WORKERS), chunk=original["chunk"])
                report["fallback"] = dict(state="running", reason=result["error"], candidate=reference_candidate)
                report["measurement_baseline"] = reference_candidate
                report["baseline_check"] = dict(state="pending", required=3, completed=0)
                if reference_candidate in remaining:
                    remaining.remove(reference_candidate)
                else:
                    total += 1
                self.log("현재 workers로 시작하지 못해 rec 기본 workers 36/32/32를 측정 기준으로 시도합니다. 저장 설정은 유지합니다.")
                result = measure(reference_candidate, "fallback_baseline", checks=3)
                report["fallback"]["state"] = "used" if result["score"] is not None else "failed"
                if result["score"] is not None:
                    chosen = {**original, **reference_candidate}
            baseline = result["score"]
            if baseline is None:
                raise Problem("측정 기준의 검증·측정이 실패했습니다. 후보를 채택하지 않고 복원합니다: " + result["error"])
            for trial in remaining:
                explore(trial, "workers")
            best = None if current_only else select_best(baseline, report["results"])
            if best:
                chosen = {**original, **best["candidate"]}
            if not current_only:
                chunks = [original["chunk"], *[v for v in plan["chunks"] if v != original["chunk"]]]
                chunk_results = []
                for chunk in chunks:
                    result = explore(dict(workers=chosen["workers"], chunk=chunk), "chunk")
                    chunk_results.append(result)
                    if len(chunk_results) == 1 and result["score"] is None:
                        raise Problem("현재 chunk의 prefill 측정 실패. 기존 설정으로 복원합니다: " + result["error"])
                best_chunk = select_best(chunk_results[0]["score"], chunk_results)
                if best_chunk:
                    chosen = {**chosen, **best_chunk["candidate"]}
                if any(chosen[key] != original[key] for key in ("workers", "chunk")):
                    final_candidate = {key: chosen[key] for key in ("workers", "chunk")}
                    report["verification"].update(state="running", candidate=final_candidate,
                                                  baseline=reference_candidate)
                    reference = measure(reference_candidate, "verify_baseline", checks=3)
                    if reference["score"] is None:
                        raise Problem("최종 비교용 기준 설정 재측정 실패: " + reference["error"])
                    final = measure(final_candidate, "verify_candidate", checks=3)
                    if final["score"] is None:
                        raise Problem("최종 조합 재측정 실패: " + final["error"])
                    reasons = []
                    for key, metric, label in (("workers", "decode_score", "생성 속도"),
                                               ("chunk", "prefill_score", "읽기 속도")):
                        old, new = reference[metric], final[metric]
                        if chosen[key] != reference_candidate[key] and new <= old * (1 + TUNING_MARGIN):
                            reasons.append(label + "가 측정 기준 대비 3% 초과 개선을 재현하지 못했습니다.")
                        elif new < old * (1 - TUNING_MARGIN):
                            reasons.append(label + "가 원래 설정 대비 3% 넘게 하락했습니다.")
                    report["adopted"] = not reasons and any(chosen[k] != reference_candidate[k] for k in ("workers", "chunk"))
                    report["recovered"] = not reasons and report["fallback"]["state"] == "used"
                    report["verification"].update(
                        state="rejected" if reasons else "passed",
                        reason=" ".join(reasons) if reasons else
                        ("rec 기본값 기준 최종 검증을 통과한 복구 제안입니다. 시작 불가한 원래 설정 대비 속도 향상은 확인하지 못했습니다."
                         if report["recovered"] else "최종 조합의 개선과 회귀 방지 기준을 통과했습니다."))
                    self.log(report["verification"]["reason"])
                    if reasons:
                        if report["fallback"]["state"] == "used":
                            raise Problem("기본값 복구 후보의 최종 검증 실패: " + " ".join(reasons))
                        chosen = original
                else:
                    total -= 2
                    report["verification"].update(state="not_needed", reason="worker·chunk 변경 후보가 없어 최종 재측정하지 않았습니다.")
                self.job["message"] = "현재 문맥 보존 · 여유 디스크 기준 SSD 캐시 용량 선택"
                free_bytes = shutil.disk_usage(self.home).free
                cache_gb = calibration_cache_gb(free_bytes)
                report["cache_selection"] = dict(
                    free_bytes=free_bytes, budget_gib=free_bytes // 5 // (2**30),
                    selected_gb=cache_gb, choices=CACHE_GB_CHOICES,
                    minimum_fallback=free_bytes < CACHE_GB_CHOICES[0] * 5 * (2**30),
                    policy="largest choice <= floor(free_bytes / 5 / 2^30), otherwise 8 GiB",
                    applied=False)
                chosen = {**chosen, "cache_gb": cache_gb}
            else:
                report["verification"].update(state="not_needed", reason="현재값만 측정하므로 최종 조합 비교와 채택을 하지 않습니다.")
            if self.cancel.is_set():
                raise Cancelled("튜닝 취소됨")
            report.update(proposal=dict(chosen), settings_after=dict(chosen))
        except BaseException as e:
            failure = e
            report.update(state="cancelled" if isinstance(e, Cancelled) else "error", error=str(e), adopted=False)
            if report["baseline_check"]["state"] == "pending":
                report["baseline_check"].update(state="failed", error=str(e))
            if report["verification"]["state"] == "running":
                report["verification"].update(state=report["state"], reason=str(e))
            if report["fallback"]["state"] == "running":
                report["fallback"]["state"] = report["state"]
            chosen = original
        finally:
            with self.tuning_lock:
                self.restoring = True
                self.benchmark_token = None
                self.cancel.clear()
            def rollback():
                errors = []
                try:
                    self.stop()
                except Exception as error:
                    errors.append("원본 복원 전 중지 실패: " + str(error))
                try:
                    self.save(original)
                except Exception as error:
                    errors.append("원본 설정 저장 실패: " + str(error))
                if was_running and not self.closing:
                    try:
                        self.start(original)
                    except Exception as error:
                        errors.append("원본 재시작 실패: " + str(error))
                if errors:
                    report.update(state="restore_error", restoration_error="; ".join(errors))
                return errors

            try:
                progress("restore", "원래 설정과 실행 상태 복원")
                self.stop()
                self.save(original)
                if was_running and not self.closing:
                    self.start(original)
            except Exception as apply_error:
                message = "측정 후 복원 실패; 원본으로 롤백: " + str(apply_error)
                if failure:
                    message = str(failure) + "; " + message
                report.update(adopted=False, state="error", error=message)
                errors = rollback()
                failure = Problem(message + ("; 복원 실패: " + "; ".join(errors) if errors else ""))
            if failure is None:
                report.update(state="done", pending=chosen != original)
            if report["state"] != "done":
                report.update(settings_after=dict(self.config), pending=False, proposal=None, recovered=False)
            if "cache_selection" in report:
                report["cache_selection"]["applied"] = False
            report["finished_at"] = time.time()
            progress("complete", "측정 완료 · 적용 대기" if report.get("pending") else "측정 종료")
            try:
                atomic(self.home / "tuning.json", json.dumps(report, indent=2))
                self.archive_tuning(report)
            except Exception as report_error:
                message = "튜닝 결과 저장 실패: " + str(report_error)
                if failure:
                    message = str(failure) + "; " + message
                report.update(state="error", adopted=False, recovered=False, pending=False, proposal=None, error=message)
                errors = rollback()
                report["settings_after"] = dict(self.config)
                if "cache_selection" in report:
                    report["cache_selection"]["applied"] = False
                failure = Problem(message + ("; 복원 실패: " + "; ".join(errors) if errors else ""))
                self.log(str(failure))
                try:
                    atomic(self.home / "tuning.json", json.dumps(report, indent=2))
                    self.archive_tuning(report)
                except Exception as retry_error:
                    failure = Problem(str(failure) + "; 실패 결과 재저장도 실패: " + str(retry_error))
        with self.tuning_lock:
            self.tuning = copy.deepcopy(report)
        if failure:
            raise failure

    def apply_tuning(self, body):
        if not isinstance(body, dict) or set(body) != {"tuning_id"}:
            raise Problem("tuning_id만 전달하세요. 설정값은 서버의 제안만 사용합니다.")
        if not self.lock.acquire(blocking=False):
            raise Problem("다른 작업이 진행 중입니다.")
        try:
            with self.tuning_lock:
                report = copy.deepcopy(self.tuning)
            if (report.get("state") != "done" or report.get("applied") is not False
                    or not report.get("pending") or not report.get("proposal")
                    or body["tuning_id"] != report.get("tuning_id")):
                raise Problem("적용 가능한 대기 제안이 없거나 결과 ID가 다릅니다.")
            before = report["settings_before"]
            if self.config != before or not self.config_file.is_file():
                raise Problem("측정 후 설정이 변경되었습니다. 다시 튜닝하세요.")
            try:
                saved = json.loads(self.config_file.read_text())
            except (OSError, ValueError) as error:
                raise Problem("저장된 설정을 확인할 수 없습니다. 설정을 확인하고 다시 튜닝하세요.") from error
            if saved != before:
                raise Problem("측정 후 설정이 변경되었습니다. 다시 튜닝하세요.")
            hardware = self.detector.snapshot()
            self.require_macos()
            environment = measurement_environment(hardware)
            previous = report.get("environment") or {}
            if any(previous.get(k) is not None and v is not None and previous[k] != v
                   for k, v in environment.items()):
                raise Problem("측정 환경이 달라졌습니다. 다시 튜닝하세요.")
            proposal = dict(validate(report["proposal"], hardware))
            if "verification" in report:
                if report.get("baseline_check", {}).get("state") != "passed":
                    raise Problem("기준 응답 재현성이 확인되지 않았습니다. 다시 튜닝하세요.")
                if any(proposal[key] != before[key] for key in ("workers", "chunk")):
                    verification = report["verification"]
                    if (verification.get("state") != "passed" or
                            verification.get("candidate") != {key: proposal[key] for key in ("workers", "chunk")}):
                        raise Problem("최종 조합 검증을 통과한 제안이 아닙니다. 다시 튜닝하세요.")
                    if ("measurement_baseline" in report and
                            verification.get("baseline") != report["measurement_baseline"]):
                        raise Problem("최종 비교의 측정 기준이 일치하지 않습니다. 다시 튜닝하세요.")
            if (report.get("cache_selection") and
                    calibration_cache_gb(shutil.disk_usage(self.home).free) < proposal["cache_gb"]):
                raise Problem("여유 디스크 기준 캐시 용량이 줄었습니다. 다시 튜닝하세요.")
            if any(v is None or previous.get(k) is None for k, v in environment.items()):
                self.log("경고: 측정 환경을 완전히 확인할 수 없는 상태에서 저장된 제안을 적용합니다.")
            was_running = self.running()
            self.cancel.clear()
            self.job = dict(state="running", action="/api/tune/apply", message="제안 적용 후 서버 시작")
            try:
                self.stop()
                self.save(proposal)
                self.start(proposal)
                updated = copy.deepcopy(report)
                updated.update(applied=True, pending=False, settings_after=dict(self.config), applied_at=time.time())
                updated.pop("apply_error", None)
                updated.pop("restoration_error", None)
                if "cache_selection" in updated:
                    updated["cache_selection"]["applied"] = True
                atomic(self.home / "tuning.json", json.dumps(updated, indent=2))
                self.archive_tuning(updated)
                with self.tuning_lock:
                    self.tuning = updated
                self.job.update(state="done", message="제안 적용 및 서버 시작 완료")
            except Exception as error:
                with self.tuning_lock:
                    self.restoring = True
                    self.cancel.clear()
                errors = []
                for label, fn in (("중지", self.stop), ("원본 설정 저장", lambda: self.save(before)),
                                  ("원본 재시작", lambda: self.start(before) if was_running and not self.closing else None)):
                    try:
                        fn()
                    except Exception as restore_error:
                        errors.append(label + " 실패: " + str(restore_error))
                message = "제안 적용 실패; 원본으로 롤백: " + str(error)
                report["apply_error"] = message
                if errors:
                    report["restoration_error"] = "; ".join(errors)
                    message += "; 복원 실패: " + report["restoration_error"]
                with self.tuning_lock:
                    self.tuning = report
                try:
                    atomic(self.home / "tuning.json", json.dumps(report, indent=2))
                    self.archive_tuning(report)
                except OSError as report_error:
                    message += "; 실패 결과 저장 실패: " + str(report_error)
                self.job.update(state="error", message=message)
                self.log(message)
                raise Problem(message) from error
        finally:
            with self.tuning_lock:
                self.restoring = False
                self.benchmark_token = None
                self.cancel.clear()
            self.lock.release()

    def cancel_work(self):
        with self.tuning_lock:
            if self.restoring:
                raise Problem("원래 설정과 실행 상태를 복원 중입니다. 복원은 취소할 수 없습니다.")
            self.cancel.set()

    def integration_status(self):
        manifest = self.read_json("integrations.json", {})
        aliases = self.read_json("aliases.json", {})
        bin_dir = Path.home() / ".local/bin"
        on_path = str(bin_dir) in [os.path.abspath(os.path.expanduser(p))
                                  for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        return {name: dict(enabled=name in manifest, command=str(self.home / "clients" / name / "launch"),
                           connection=Connections(self.home, self.config).status(name)
                           if name in ("opencode", "pi") else None,
                           alias=dict(enabled=name in aliases, command=name + "-flint",
                                      path=str(bin_dir / (name + "-flint")), on_path=on_path))
                for name in ("claude", "codex", "opencode", "pi")}

    def connection(self, name, enable):
        try:
            path = Connections(self.home, self.config).change(name, enable)
        except ConfigError as error:
            raise Problem(str(error)) from error
        self.log(f"{name}: Flint 항목 {'추가 / 갱신' if enable else '제거'} 완료 ({path}). "
                 "다른 모델·기본 모델·로그인은 유지했습니다.")

    def alias(self, name, enable):
        if name not in ("claude", "codex", "opencode", "pi") or type(enable) is not bool:
            raise Problem("알 수 없는 별칭")
        aliases = self.read_json("aliases.json", {})
        path = Path.home() / ".local/bin" / (name + "-flint")
        target = self.home / "clients" / name / "launch"
        if enable:
            if name in aliases:
                raise Problem("이미 설치된 별칭입니다. 먼저 제거하세요.")
            integration = self.read_json("integrations.json", {}).get(name)
            if integration is None:
                raise Problem("먼저 전용 실행기를 생성하세요.")
            if (target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK)
                    or digest(target) != integration["installed"]):
                raise Problem("전용 실행기 파일을 확인하세요.")
            if os.path.lexists(path):
                raise Problem("같은 이름의 파일이 있습니다. 기존 파일을 보호하기 위해 설치를 거부합니다.")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.symlink_to(target)
            except FileExistsError as error:
                raise Problem("같은 이름의 파일이 생성되었습니다. 기존 파일을 유지합니다.") from error
            aliases[name] = str(target)
            try:
                atomic(self.home / "aliases.json", json.dumps(aliases, indent=2))
            except OSError:
                if path.is_symlink() and os.readlink(path) == str(target):
                    path.unlink()
                raise
        else:
            expected = aliases.get(name)
            if expected is None:
                raise Problem("제거할 별칭이 없습니다.")
            if not path.is_symlink() or os.readlink(path) != expected:
                raise Problem("별칭이 이후 변경되었거나 없습니다. 최신 변경을 보호하기 위해 제거를 거부합니다.")
            path.unlink()
            del aliases[name]
            atomic(self.home / "aliases.json", json.dumps(aliases, indent=2))
        self.log(f"{name}-flint: 별칭 {'설치' if enable else '제거'} 완료. 셸 설정과 기본 CLI는 변경하지 않았습니다.")

    def integration(self, name, enable):
        if name not in ("claude", "codex", "opencode", "pi") or type(enable) is not bool:
            raise Problem("알 수 없는 연동")
        import shlex
        import sys
        path = self.home / "clients" / name / "launch"
        manifest = self.read_json("integrations.json", {})
        if enable:
            if name in manifest:
                raise Problem("이미 생성된 연동입니다. 먼저 복원하세요.")
            previous = path.read_text() if path.exists() else None
            text = "#!/bin/sh\nexec " + " ".join(shlex.quote(s) for s in
                    (sys.executable, str(ROOT / "flint/client.py"), name, str(self.home))) + ' "$@"\n'
            atomic(path, text, 0o700)
            manifest[name] = dict(before=previous, installed=digest(path))
        else:
            entry = manifest.get(name)
            if not entry:
                raise Problem("복원할 연동이 없습니다.")
            if name in self.read_json("aliases.json", {}):
                raise Problem("먼저 전용 명령 별칭을 제거하세요.")
            if not path.is_file() or digest(path) != entry["installed"]:
                raise Problem("연동 파일이 이후 수정되었습니다. 최신 변경을 보호하기 위해 복원을 거부합니다.")
            if entry["before"] is None:
                path.unlink()
            else:
                atomic(path, entry["before"], 0o700)
            del manifest[name]
        atomic(self.home / "integrations.json", json.dumps(manifest, indent=2))
        self.log(f"{name}: 전용 실행기 {'생성' if enable else '복원'} 완료. 기존 클라이언트 설정은 변경하지 않았습니다.")
