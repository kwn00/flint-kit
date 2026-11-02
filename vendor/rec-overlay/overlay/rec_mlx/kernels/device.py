# Modified from recursive-oss/rec 0.1.1 (Apache-2.0) for bable-flint: M4 Max (40-core GPU) workers 76/64/64, REC_EXP_WORKERS and REC_EXP_CHUNK. See NOTICE.
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceProfile:
    chip: str
    architecture: str
    workers: int = 36
    mlp_workers: int = 32
    head_workers: int = 32
    prefill_chunk_size: int = 2048
    specialize_mixed_experts: bool = True


def profile_for_device(info):
    return _exp_override(_profile_for_device(info))


def _profile_for_device(info):
    chips = {
        ("applegpu_g16s", "Apple M4 Pro"): "M4 Pro",
        ("applegpu_g16s", "Apple M4 Max"): "M4 Max",
        ("applegpu_g17s", "Apple M5 Pro"): "M5 Pro",
        ("applegpu_g17s", "Apple M5 Max"): "M5 Max",
    }
    architecture = info["architecture"]
    identity = (architecture, info["device_name"])
    if identity not in chips:
        raise ValueError(f"Unsupported resident kernel device: {identity}")
    if chips[identity] == "M5 Max":
        return DeviceProfile(
            chip=chips[identity],
            architecture=architecture,
            workers=76,
            mlp_workers=64,
            head_workers=64,
            prefill_chunk_size=4096,
        )
    return DeviceProfile(
        chip=chips[identity],
        architecture=architecture,
        prefill_chunk_size=4096 if chips[identity] == "M5 Pro" else 2048,
        # The specialized M4 resident shader fails scalar/packet parity.
        specialize_mixed_experts=chips[identity] not in ("M4 Pro", "M4 Max"),
    )


def gpu_core_count():
    """The GPU core count ioreg reports (AGXAccelerator's gpu-core-count), or None."""
    import re, subprocess

    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rc", "AGXAccelerator", "-d", "1"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"gpu-core-count"\s*=\s*(\d+)', out)
    return int(found.group(1)) if found else None


def _exp_override(profile, cores=None):
    import dataclasses, os

    changes = {}
    if profile.chip == "M4 Max":  # tuned on M4 Max 40-core, macOS 27.2
        # rec's 2048-token prefill chunk: 4096 read no faster, and prefix checkpoints sit at chunk starts.
        # The resident decode kernel needs every worker on the GPU at once, so 76 is kept to the 40-core chip it
        # was measured on; a 32-core M4 Max (or an unknown count) keeps rec's own 36/32/32.
        if (cores if cores is not None else gpu_core_count() or 0) >= 40:
            changes.update(workers=76, mlp_workers=64, head_workers=64)
    if os.environ.get("REC_EXP_CHUNK"):
        changes["prefill_chunk_size"] = int(os.environ["REC_EXP_CHUNK"])
    if os.environ.get("REC_EXP_WORKERS"):
        w, m, h = (int(x) for x in os.environ["REC_EXP_WORKERS"].split(","))
        changes.update(workers=w, mlp_workers=m, head_workers=h)
    return dataclasses.replace(profile, **changes)
