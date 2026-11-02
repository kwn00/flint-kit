#!/usr/bin/env python3
# Modified for Flint Local (2026-10-30): standalone data path, setup messages and macOS 15 check.
# Branding updated for Flint Kit (2026-11-02); the existing data path is unchanged.
# Upstream attribution and license are preserved in NOTICE and LICENSE.
"""Build the tuned Flint runtime (M4 Pro/Max, M5 Pro/Max) in Flint Kit's folder, FLINT_HOME
($FLINT_HOME, else ~/.local/share/flint-local): rec 0.1.1's runtime with overlay/rec_mlx on top, a manifest that
matches it, and the FLINT_HOME/bin/rec-serve-tuned launcher.

The app puts rec 0.1.1 at FLINT_HOME/bin/rec first. rec runs with XDG_DATA_HOME=FLINT_HOME, so its models, runtimes
and key live in FLINT_HOME/rec. This installer never modifies the user's own rec.
The controller may APFS-clone existing rec model files into FLINT_HOME before installation.

  python3 install.py          install: unpack rec's runtime from FLINT_HOME/bin/rec if it is not unpacked yet, then
                              build FLINT_HOME/rec-tuned (the previous one is kept as rec-tuned.bak-<time>; older
                              backups, and a rec-tuned.new left by a killed install, go to the Trash)
  python3 install.py --check  only check this Mac, rec and its runtime; unpacks nothing
                              (a runtime not unpacked yet is found and verified inside the binary instead)

A Mac below Flint's minimum (an M4 Pro/Max or M5 Pro/Max, 48 GiB of memory, room on disk) is refused before
anything is touched.

Standard library only; runs with any python3 on the Mac."""

import argparse
import hashlib
import json
import mmap
import os
import pathlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib

REC_VERSION = "rec 0.1.1"
SITE = "python/lib/python3.12/site-packages"
HERE = pathlib.Path(__file__).resolve().parent
OVERLAY = HERE / "overlay"
HOME = pathlib.Path.home()
FLINT_HOME = pathlib.Path(os.environ.get("FLINT_HOME") or HOME / ".local/share/flint-local")
REC = FLINT_HOME / "bin/rec"
MODELS = FLINT_HOME / "rec/models"        # rec's data, with XDG_DATA_HOME=FLINT_HOME
RUNTIMES = FLINT_HOME / "rec/runtimes"
TARGET = FLINT_HOME / "rec-tuned"
LAUNCHER = FLINT_HOME / "bin/rec-serve-tuned"
TRASH = HOME / ".Trash"
CHUNK = 1 << 20

# rec 0.1.1's own limits: kernels/device.py knows only these chips, engine.py stops below 48 GiB.
CHIPS = ("Apple M4 Pro", "Apple M4 Max", "Apple M5 Pro", "Apple M5 Max")
MIN_MEMORY = 48 * 1024**3
# Disk: the model takes 21 GiB and both runtimes 2.3 GB; the conversation cache needs at least 8 GiB more.
MIN_FREE_WITH_MODEL = 10 * 1024**3
MIN_FREE_WITHOUT_MODEL = 32 * 1024**3


def fail(message):
    sys.exit(f"install.py: {message}")


def unmet(system, machine, chip, memory, free, has_model, macos):
    """What keeps this Mac from running Flint, as sentences; empty when it can."""
    if system != "Darwin" or machine != "arm64":
        return ["Flint needs an Apple Silicon Mac"]
    problems = []
    if not isinstance(macos, str) or not re.fullmatch(r"\d+(?:\.\d+){1,2}", macos):
        problems.append("cannot determine macOS version; macOS 15 or newer is required")
    elif int(macos.split(".")[0]) < 15:
        problems.append(f"macOS {macos}; macOS 15 or newer is required")
    if chip not in CHIPS:
        problems.append(f"{chip or 'this chip'} is not supported; Flint runs on an M4 Pro, M4 Max, M5 Pro or M5 Max")
    if memory < MIN_MEMORY:
        problems.append(f"{memory / 1024**3:.0f} GB of memory; Flint needs at least 48 GB")
    need = MIN_FREE_WITH_MODEL if has_model else MIN_FREE_WITHOUT_MODEL
    if free < need:
        problems.append(f"{free / 1024**3:.0f} GB free on disk; at least {need // 1024**3} GB is needed"
                        + ("" if has_model else " (the model alone is 21 GB)"))
    return problems


def this_mac():
    import platform

    def sysctl(name):
        result = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""

    memory = sysctl("hw.memsize")
    has_model = MODELS.is_dir() and any((d / "config.json").is_file() for d in MODELS.iterdir())
    return dict(system=platform.system(), machine=platform.machine(), chip=sysctl("machdep.cpu.brand_string"),
                memory=int(memory) if memory.isdigit() else 0, free=shutil.disk_usage(HOME).free,
                has_model=has_model, macos=platform.mac_ver()[0])


def require_supported_mac():
    facts = this_mac()
    problems = unmet(**facts)
    if problems:
        fail("this Mac cannot run Flint, so nothing was installed:\n  - " + "\n  - ".join(problems))
    return facts


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def rec_env():
    return {**os.environ, "XDG_DATA_HOME": str(FLINT_HOME)}


def rec_binary():
    if not REC.is_file():
        fail(f"{REC_VERSION} is missing at {REC}; install the verified official rec binary first")
    version = subprocess.run([str(REC), "--version"], capture_output=True, text=True, env=rec_env()).stdout.strip()
    if version != REC_VERSION:
        fail(f"{REC} must be {REC_VERSION}, found {version or 'no version'}")
    return REC


def mapped(binary):
    with open(binary, "rb") as file:
        return mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)


def embedded_manifest(binary):
    """rec carries its runtime manifest as JSON inside the binary. Walk back from its last key to the
    outermost object that parses."""
    with mapped(binary) as data:
        key = data.find(b'"runtime_sha256"')
        if key < 0:
            fail(f"no runtime manifest inside {binary}")
        end = data.find(b"}", key) + 1
        start = data.rfind(b"{\n", 0, key)
        while start >= 0:
            try:
                manifest = json.loads(data[start:end])
            except ValueError:
                start = data.rfind(b"{\n", 0, start)
                continue
            if "runtime_files" in manifest:
                return manifest
            start = data.rfind(b"{\n", 0, start)
    fail(f"could not read the runtime manifest inside {binary}")


def gzip_end(data, start):
    """Where the gzip stream at `start` ends, or None when it is not one (stops at the first zlib error)."""
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    pos = start
    try:
        while pos < len(data):
            chunk = data[pos:pos + CHUNK]
            stream.decompress(chunk, CHUNK)   # output is dropped; max_length keeps a bomb from filling memory
            while stream.unconsumed_tail and not stream.eof:
                stream.decompress(stream.unconsumed_tail, CHUNK)
            if stream.eof:
                return pos + len(chunk) - len(stream.unused_data)
            pos += len(chunk)
    except zlib.error:
        return None
    return None


def runtime_archive(data, manifest):
    """The (start, end) of rec's embedded assets/runtime.tar.gz: the gzip stream whose bytes hash to
    runtime_sha256 (rec embeds the blob as is)."""
    pos = data.find(b"\x1f\x8b\x08")
    while pos >= 0:
        end = gzip_end(data, pos)
        if end is not None:
            digest = hashlib.sha256()
            for at in range(pos, end, CHUNK):
                digest.update(data[at:min(at + CHUNK, end)])
            if digest.hexdigest() == manifest["runtime_sha256"]:
                return pos, end
        pos = data.find(b"\x1f\x8b\x08", pos + 1)
    return None


class Span:
    """A read-only file over data[start:end], for tarfile's stream mode."""

    def __init__(self, data, start, end):
        self.data, self.pos, self.end = data, start, end

    def read(self, size=-1):
        stop = self.end if size is None or size < 0 else min(self.pos + size, self.end)
        chunk = self.data[self.pos:stop]
        self.pos += len(chunk)
        return chunk


def safe_name(name):
    return (bool(name) and not name.startswith("/") and "\\" not in name and "\0" not in name
            and all(part not in ("", ".", "..") for part in name.split("/")))


def runtime_problem(root, manifest):
    """Why `root` is not the runtime the manifest lists, or None (rec's verifyRuntime: every listed file is a
    regular file with its size and sha256, and executable when listed so)."""
    for entry in manifest["runtime_files"]:
        name = entry["name"]
        try:
            info = (root / name).lstat()
        except OSError:
            return f"{name} is missing"
        if not stat.S_ISREG(info.st_mode) or info.st_size != entry["size"]:
            return f"{name} is not the listed {entry['size']}-byte file"
        # Windows (tests only) has no x bit.
        if entry.get("executable") and os.name == "posix" and not info.st_mode & 0o100:
            return f"{name} is not executable"
        if file_sha256(root / name) != entry["sha256"]:
            return f"{name} does not match its sha256"
    return None


def make_dirs(root, relative):
    path = root
    for part in relative.parts:
        path = path / part
        if not path.is_dir():
            path.mkdir(mode=0o700)


def unpack(data, span, manifest, root):
    """rec's unpack: every entry a regular, listed, safe, not repeated file of the listed size; files 0600
    (0700 executable), folders 0700."""
    entries = {entry["name"]: entry for entry in manifest["runtime_files"]}
    seen = set()
    try:
        with tarfile.open(fileobj=Span(data, *span), mode="r|gz") as archive:
            for member in archive:
                name = member.name
                entry = entries.get(name)
                if not member.isreg() or entry is None or name in seen or not safe_name(name):
                    fail(f"rec's runtime archive has an unexpected entry {name!r}")
                if member.size != entry["size"]:
                    fail(f"{name} in rec's runtime archive is {member.size} bytes, the manifest says {entry['size']}")
                seen.add(name)
                make_dirs(root, pathlib.PurePosixPath(name).parent)
                mode = 0o700 if entry.get("executable") else 0o600
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                with archive.extractfile(member) as source, os.fdopen(os.open(root / name, flags, mode), "wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
    except (tarfile.TarError, zlib.error, EOFError) as error:
        fail(f"rec's runtime archive could not be read: {error}")
    missing = sorted(entries.keys() - seen)
    if missing:
        fail(f"rec's runtime archive lacks {missing[0]}" + (f" and {len(missing) - 1} more" if len(missing) > 1 else ""))
    problem = runtime_problem(root, manifest)
    if problem:
        fail(f"rec's runtime did not unpack cleanly: {problem}")


def extract_lock():
    """rec's own lock (RUNTIMES/extract.lock, flock) so a `rec serve` on FLINT_HOME does not unpack at the
    same time; nothing on Windows (tests only)."""
    try:
        import fcntl
    except ImportError:
        return None
    lock = os.open(RUNTIMES / "extract.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def stock_runtime(binary, manifest, extract):
    """rec's own runtime at RUNTIMES/<runtime_sha256>, unpacked from the binary like `rec serve` does when it is
    not there yet. With extract=False nothing is written: a missing runtime is only found and verified inside the
    binary, and None is returned."""
    stock = RUNTIMES / manifest["runtime_sha256"]
    if not extract and not stock.exists():
        with mapped(binary) as data:
            if runtime_archive(data, manifest) is None:
                fail(f"no runtime archive matching runtime_sha256 inside {binary}")
        print(f"rec's runtime is not unpacked yet; installing unpacks it from {binary} into {stock}")
        return None
    if extract:
        make_dirs(pathlib.Path(RUNTIMES.anchor), RUNTIMES.relative_to(RUNTIMES.anchor))
    lock = extract_lock() if extract else None
    try:
        if stock.exists():
            problem = runtime_problem(stock, manifest)
            if problem:
                fail(f"cached runtime is corrupt ({problem}); remove {stock} and retry")
            return stock
        with mapped(binary) as data:
            span = runtime_archive(data, manifest)
            if span is None:
                fail(f"no runtime archive matching runtime_sha256 inside {binary}")
            staging = pathlib.Path(tempfile.mkdtemp(prefix=".extract-", dir=RUNTIMES))
            try:
                unpack(data, span, manifest, staging)
                staging.rename(stock)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        print(f"unpacked rec's runtime into {stock}")
        return stock
    finally:
        if lock is not None:
            os.close(lock)


def overlay_files():
    return sorted(p for p in OVERLAY.rglob("*.py") if "__pycache__" not in p.parts)


def launcher_text():
    runtime = TARGET / "rt"
    return ("#!/bin/zsh\n"
            "# rec serve on Flint Kit's isolated runtime and data.\n"
            f"export XDG_DATA_HOME={shlex.quote(str(FLINT_HOME))}\n"
            f"exec {shlex.quote(str(REC))} serve --manifest {shlex.quote(str(TARGET / 'manifest.json'))}"
            f' --runtime-dir {shlex.quote(str(runtime))} "$@"\n')


def install(manifest, stock, entries):
    # Build beside the live install and swap at the end, so a failed copy leaves the previous one in use.
    staging = TARGET.with_name(f"{TARGET.name}.new")
    if staging.exists():
        # Left by an install that was killed (power loss, SIGKILL): a half-built copy, so it goes to the Trash.
        target = trash_name(TRASH, staging.name)
        try:
            staging.rename(target)
        except OSError as error:
            fail(f"{staging} is left from a failed install and could not be moved to the Trash ({error}); "
                 "move it away and run again")
        print(f"moved {staging.name}, left from a failed install, to the Trash")
    staging.mkdir(parents=True)
    try:
        build(staging, manifest, stock, entries)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if TARGET.exists():
        backup = TARGET.with_name(f"{TARGET.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        TARGET.rename(backup)
        print(f"kept the previous install as {backup}")
        trash_older_backups(keep=backup, trash=TRASH)
    staging.rename(TARGET)
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER.write_text(launcher_text())
    LAUNCHER.chmod(0o755)
    print(f"installed {TARGET} and {LAUNCHER}")


def trash_name(trash, name):
    """A path in the Trash for `name` that nothing is at yet."""
    target = trash / name
    stamp = time.strftime('%H-%M-%S')
    number = 1
    while target.exists():
        target = trash / (f"{name} {stamp}" if number == 1 else f"{name} {stamp} {number}")
        number += 1
    return target


def trash_older_backups(keep, trash=TRASH):
    """Moves every rec-tuned.bak-* but `keep` to the Trash, so reinstalling keeps one backup; one that cannot be
    moved stays where it is."""
    for old in sorted(TARGET.parent.glob(f"{TARGET.name}.bak-*")):
        if old == keep:
            continue
        target = trash_name(trash, old.name)
        try:
            old.rename(target)
            print(f"moved {old.name} to the Trash")
        except OSError as error:
            print(f"left {old}: {error}")


def build(root, manifest, stock, entries):
    runtime = root / "rt"
    # APFS clone: the 1 GB runtime copy shares blocks with rec's own until a file changes.
    subprocess.run(["cp", "-cR", str(stock), str(runtime)], check=True)
    for path in overlay_files():
        name = f"{SITE}/{path.relative_to(OVERLAY).as_posix()}"
        data = path.read_bytes()
        (runtime / name).write_bytes(data)
        entry = entries.get(name)
        if entry is None:
            entry = {"name": name, "executable": False}
            manifest["runtime_files"].append(entry)
        entry.update(size=len(data), sha256=sha256(data))
    shutil.rmtree(runtime / SITE / "rec_mlx" / "__pycache__", ignore_errors=True)
    shutil.rmtree(runtime / SITE / "rec_mlx" / "kernels" / "__pycache__", ignore_errors=True)
    manifest["development"] = True
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="check only, change nothing")
    args = parser.parse_args()
    require_supported_mac()
    binary = rec_binary()
    manifest = embedded_manifest(binary)
    # Every file of the stock runtime is verified, so the originals the overlay replaces are rec's own.
    stock = stock_runtime(binary, manifest, extract=not args.check)
    entries = {entry["name"]: entry for entry in manifest["runtime_files"]}
    if args.check:
        print(f"ok: this Mac and {REC_VERSION} can run the tuned runtime")
        return
    install(manifest, stock, entries)


if __name__ == "__main__":
    main()
