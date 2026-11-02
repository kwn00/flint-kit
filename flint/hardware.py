"""Read-only Mac detection and pure calibration policy; no model/runtime imports."""
import math
import platform
import re
import subprocess
import threading
import time

SUPPORTED_CHIPS = ("Apple M4 Pro", "Apple M4 Max", "Apple M5 Pro", "Apple M5 Max")
CHUNKS = (1024, 2048, 4096)
REC_DEFAULT_WORKERS = (36, 32, 32)
GIB = 2**30


class HardwareDetector:
    def __init__(self, runner=subprocess.run, system=platform.system, macos=platform.mac_ver,
                 clock=time.monotonic, retry_seconds=60, max_attempts=3):
        self.runner, self.system, self.macos, self.clock = runner, system, macos, clock
        self.retry_seconds, self.max_attempts = retry_seconds, max_attempts
        self.lock = threading.Lock()
        self.values = {}
        self.reasons = {}
        self.attempts = {}
        self.next_try = {}

    def _read(self, field):
        if self.system() != "Darwin":
            raise ValueError("macOS가 아니므로 감지할 수 없습니다.")
        if field == "macos":
            value = self.macos()[0]
            if not re.fullmatch(r"\d+(?:\.\d+){1,2}", value):
                raise ValueError("macOS 버전 형식이 올바르지 않습니다.")
            return value
        commands = {
            "arm64": ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            "chip": ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
            "memory_bytes": ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            "gpu_cores": ["/usr/sbin/ioreg", "-rc", "AGXAccelerator", "-d", "1"],
        }
        result = self.runner(commands[field], capture_output=True, text=True, timeout=5,
                             env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"},
                             shell=False)
        if result.returncode != 0:
            raise ValueError("감지 명령 종료 코드: " + str(result.returncode))
        text = result.stdout.strip()
        if len(text) > 1024 * 1024:
            raise ValueError("감지 출력이 너무 큽니다.")
        if field == "chip":
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ()+._-]{0,95}", text):
                raise ValueError("칩 이름 형식이 올바르지 않습니다.")
            return text
        if field == "gpu_cores":
            matches = re.findall(r'^\s*"gpu-core-count"\s*=\s*(\d+)\s*$', text, re.MULTILINE)
            if not matches or len(set(matches)) != 1:
                raise ValueError("AGXAccelerator의 단일 gpu-core-count를 읽지 못했습니다.")
            text = matches[0]
        if not re.fullmatch(r"(?:0|[1-9]\d{0,15})", text):
            raise ValueError("감지 정수 형식이 올바르지 않습니다.")
        value = int(text)
        if field == "arm64":
            if value not in (0, 1):
                raise ValueError("arm64 값은 0 또는 1이어야 합니다.")
            return bool(value)
        if not 1 <= value <= (512 if field == "gpu_cores" else 2**50):
            raise ValueError("감지 값 범위가 올바르지 않습니다.")
        return value

    def snapshot(self):
        with self.lock:
            now = self.clock()
            for field in ("arm64", "chip", "gpu_cores", "memory_bytes", "macos"):
                if (field in self.values or self.attempts.get(field, 0) >= self.max_attempts
                        or now < self.next_try.get(field, 0)):
                    continue
                self.attempts[field] = self.attempts.get(field, 0) + 1
                self.next_try[field] = now + self.retry_seconds
                try:
                    self.values[field] = self._read(field)
                    self.reasons.pop(field, None)
                except subprocess.TimeoutExpired:
                    self.reasons[field] = "감지 명령이 5초 안에 완료되지 않았습니다."
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.reasons[field] = str(error)
            return {**{field: self.values.get(field) for field in
                       ("arm64", "chip", "gpu_cores", "memory_bytes", "macos")},
                    "reasons": dict(self.reasons)}


DETECTOR = HardwareDetector()


def defaults(hardware):
    chip, cores = hardware.get("chip"), hardware.get("gpu_cores")
    large = chip == "Apple M5 Max" or (chip == "Apple M4 Max" and cores is not None and cores >= 40)
    return dict(workers=[76, 64, 64] if large else [36, 32, 32],
                chunk=4096 if chip and chip.startswith("Apple M5") else 2048)


def worker_candidates(hardware):
    cores = hardware.get("gpu_cores")
    if hardware.get("chip") not in SUPPORTED_CHIPS or cores is None:
        return []
    decode_default, mlp_default, _ = defaults(hardware)["workers"]
    cap = max(decode_default, int(cores * 1.9) // 4 * 4)
    start = max(32, math.floor(cores / 4 + 0.5) * 4)
    decode = sorted({*range(start, cap + 1, 8), cap, decode_default})
    mlp_cap = max(mlp_default, int(cores * 1.6) // 4 * 4)
    return [[d, m, m] for d in decode if d <= cap for m in (32, 64) if m <= mlp_cap and m <= d]


def worker_options(hardware):
    options = worker_candidates(hardware)
    default = defaults(hardware)["workers"]
    for workers in (default, list(REC_DEFAULT_WORKERS)):
        if workers not in options:
            options.append(workers)
    return sorted(options)


def worker_validity(workers, hardware):
    if workers == defaults(hardware)["workers"]:
        warning = ("칩 또는 GPU 코어를 읽지 못했습니다. 기본 workers로 시작할 수 있지만 튜닝은 제한됩니다."
                   if hardware.get("chip") is None or hardware.get("gpu_cores") is None else "")
        return dict(state="valid", reason=warning)
    if hardware.get("chip") is None or hardware.get("gpu_cores") is None:
        return dict(state="unverified", reason="칩 또는 GPU 코어를 읽지 못해 저장된 비기본 workers를 검증할 수 없습니다.")
    if workers not in worker_options(hardware):
        return dict(state="invalid", reason="저장된 workers가 감지된 칩·GPU 코어의 허용 후보에 없습니다. 자동 교정하지 않습니다.")
    return dict(state="valid", reason="")


def macos_requirement(hardware):
    version = hardware.get("macos")
    known = isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+){1,2}", version)
    passed = int(version.split(".")[0]) >= 15 if known else None
    reason = ("" if passed else f"현재 macOS {version}은 지원하지 않습니다." if known else
              hardware.get("reasons", {}).get("macos", "macOS 버전을 확인할 수 없습니다."))
    return dict(name="macOS 15 이상", passed=passed, reason=reason)


def requirements(hardware, free_bytes, models):
    memory = hardware.get("memory_bytes")
    minimum = 10 if models else 32
    checks = [
        dict(name="Apple Silicon", passed=hardware.get("arm64"),
             reason=hardware.get("reasons", {}).get("arm64", "감지하지 못했습니다." if hardware.get("arm64") is None else "Apple Silicon이 아닙니다.")),
        dict(name="지원 칩", passed=None if hardware.get("chip") is None else hardware["chip"] in SUPPORTED_CHIPS,
             reason=hardware.get("reasons", {}).get("chip", "감지하지 못했습니다." if hardware.get("chip") is None else "M4 Pro/Max 또는 M5 Pro/Max가 필요합니다.")),
        dict(name="통합 메모리 48 GiB 이상", passed=None if memory is None else memory >= 48 * GIB,
             reason=hardware.get("reasons", {}).get("memory_bytes", "감지하지 못했습니다." if memory is None else "통합 메모리가 48 GiB 미만입니다.")),
        macos_requirement(hardware),
        dict(name=f"데이터 볼륨 여유 {minimum} GiB 이상", passed=free_bytes >= minimum * GIB,
             reason="모델 있음" if models else "모델 없음 · 모델 다운로드 공간 포함"),
    ]
    for check in checks:
        if check["passed"] is True:
            check["reason"] = ""
    return checks


def tune_reason(hardware):
    for check in requirements(hardware, 32 * GIB, False)[:-1]:
        if check["passed"] is not True:
            return check["name"] + ": " + check["reason"]
    if hardware.get("gpu_cores") is None:
        return "GPU 코어 알 수 없음: " + hardware.get("reasons", {}).get("gpu_cores", "감지하지 못했습니다.")
    return ""


def measurement_environment(hardware):
    memory = hardware.get("memory_bytes")
    return dict(chip=hardware.get("chip"), gpu_cores=hardware.get("gpu_cores"),
                memory_gib=memory / GIB if memory is not None else None, macos=hardware.get("macos"))


def tuning_plan(hardware, current_workers):
    reason = tune_reason(hardware)
    validity = worker_validity(current_workers, hardware)
    if validity["state"] == "invalid":
        reason = validity["reason"]
    workers = [] if reason else [current_workers, *[w for w in worker_candidates(hardware) if w != current_workers]]
    return dict(workers=workers, chunks=list(CHUNKS), worker_count=len(workers),
                chunk_count=len(CHUNKS), allowed=not reason, reason=reason)


def hung_dominates(failed, candidate, phase):
    if phase == "workers":
        return (candidate["chunk"] == failed["chunk"]
                and candidate["workers"] != failed["workers"]
                and all(v >= threshold for v, threshold in zip(candidate["workers"], failed["workers"])))
    return (phase == "chunk" and candidate["workers"] == failed["workers"]
            and candidate["chunk"] > failed["chunk"])
