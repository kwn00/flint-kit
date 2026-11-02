# Added by bable-flint (not part of recursive-oss/rec): prompt cache on disk. Apache-2.0, see NOTICE.
"""Prompt caches on disk, so a conversation survives a restart or another conversation taking the RAM cache.

After a request whose prompt is at least REC_DISK_CACHE_MIN tokens (default 8192) and at least 4096 tokens past
the longest stored prefix of it, the engine stores that prompt's state: the used BF16 global KV, the sliding
windows, the last eight prefix checkpoints and the final logits. The shorter stored prefix is then deleted, so a
growing conversation keeps one file. A later prompt that starts with a stored prompt, further than the RAM cache
reaches, loads it instead of reading those tokens again. Files go to REC_DISK_CACHE (default ~/.cache/rec-kv; 0
turns this off) in one folder per model, oldest first out (of all those folders) past REC_DISK_CACHE_GB (default 32)."""

import hashlib
import logging
import os
import re
import shutil
import threading
import time
from array import array
from pathlib import Path

import mlx.core as mx

from .cache import FixedSlidingKVCache, copy_array
from .prefix import Checkpoint, PromptCache

STEP_TOKENS = 4096
CHECKPOINTS = 8
# The global KV is saved in pieces of at most this many values: MLX overflows on one array past 2**31 values,
# which a prompt reaches at about 130K tokens.
PIECE_VALUES = 1 << 29


def _places(sizes, limit=PIECE_VALUES):
    """(piece, offset) of each run when the runs fill pieces of at most `limit` values in order; a run is never
    split."""
    piece, filled, places = 0, 0, []
    for size in sizes:
        if filled and filled + size > limit:
            piece, filled = piece + 1, 0
        places.append((piece, filled))
        filled += size
    return places


def _key(tokens):
    return hashlib.sha256(array("I", tokens).tobytes()).hexdigest()[:32]


def _ranges(storage, count):
    """(bank, first element, positions) runs holding global KV positions [0, count) for every global layer,
    K/V and head, in order; one run per 65536-token bank."""
    for row in storage.rows[:49]:
        if not row[1]:
            continue
        cap = row[62]
        for kind in range(2):
            for head in range(4):
                base = row[61] + kind * cap * 512 + head * cap * 128
                for bank, begin in enumerate(range(0, count, cap)):
                    yield bank, base, min(cap, count - begin)


def open_disk_cache(identity):
    directory = os.environ.get("REC_DISK_CACHE", str(Path.home() / ".cache" / "rec-kv"))
    if directory == "0":
        return None
    # KV computed by other weights must never load: one folder per model, weight and KV format.
    model = "-".join(str(identity[name]) for name in ("model_id", "weight_format", "kv_format"))
    folder = Path(directory) / re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    return DiskCache(folder, int(float(os.environ.get("REC_DISK_CACHE_GB", 32)) * 2**30))


class DiskCache:
    def __init__(self, directory, limit_bytes):
        self.directory, self.limit_bytes = directory, limit_bytes
        self.minimum = int(os.environ.get("REC_DISK_CACHE_MIN", 8192))
        # The files hold conversations: the folder and the files are for this user only.
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)  # one made before this was set
        except OSError as error:
            logging.warning("Disk prompt cache: could not make %s private: %s", directory, error)
        for stale in directory.glob("*.tmp.safetensors"):
            stale.unlink()
        # key -> prompt length, from the file names (<length>-<key>.safetensors). The GPU worker reads and saves,
        # the writer thread drops entries; both hold the lock.
        self.lock = threading.Lock()
        self.entries = {}
        self.pending = None  # the key being written, whose file is not there yet
        for path in directory.glob("*.safetensors"):
            length, _, key = path.stem.partition("-")
            if length.isdigit() and key:
                self.entries[key] = int(length)
        self.writer = None

    def _path(self, key, length):
        return self.directory / f"{length}-{key}.safetensors"

    def _drop(self, key, length):
        with self.lock:
            self.entries.pop(key, None)
        self._path(key, length).unlink(missing_ok=True)

    def find(self, ids, longer_than):
        with self.lock:
            entries = list(self.entries.items())
        best = None
        for key, length in entries:
            if longer_than < length <= len(ids) and (best is None or length > best[1]):
                # A file can be gone (the app cleared the cache) while the server runs.
                if _key(ids[:length]) == key and (key == self.pending or self._path(key, length).exists()):
                    best = key, length
        return best

    def save(self, cache):
        tokens = cache.tokens
        if len(tokens) < self.minimum or not mx.all(mx.isfinite(cache.logits)).item():
            return
        previous = self.find(tokens, 0)
        if previous is not None and len(tokens) - previous[1] < STEP_TOKENS:
            return
        # The previous save's copies go first, so at most two copies of the KV are held, not three.
        self.wait()
        storage = cache.storage
        banks = [bank.reshape(-1) for bank in storage.banks]
        runs = [banks[bank][first : first + count * 128] for bank, first, count in _ranges(storage, len(tokens))]
        needed = sum(run.size for run in runs) * 2
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)  # cleared while the server runs
        free = shutil.disk_usage(self.directory).free
        if free < 2 * needed:
            logging.warning("Disk prompt cache: %d GiB free, not saving %d tokens", free >> 30, len(tokens))
            return
        started = time.perf_counter()
        places = _places([run.size for run in runs])
        pieces = [[] for _ in range(places[-1][0] + 1)]
        for run, (piece, _) in zip(runs, places):
            pieces[piece].append(run)
        arrays = {
            "tokens": mx.array(tokens, mx.uint32),
            **{f"global.{number}": mx.concatenate(piece) for number, piece in enumerate(pieces)},
            "logits": copy_array(cache.logits),
        }
        for index, layer in enumerate(cache.layers):
            if isinstance(layer, FixedSlidingKVCache):
                keys, values = layer._prior()
                arrays[f"sliding.{index}.keys"] = copy_array(keys)
                arrays[f"sliding.{index}.values"] = copy_array(values)
        checkpoints = cache.checkpoints[-CHECKPOINTS:]
        for number, checkpoint in enumerate(checkpoints):
            arrays[f"checkpoint.{number}.state"] = mx.array(
                [checkpoint.position] + [v for _, _, valid, ring in checkpoint.sliding for v in (valid, ring)],
                mx.uint32,
            )
            arrays[f"checkpoint.{number}.logits"] = copy_array(checkpoint.logits)
            for slot, (keys, values, _, _) in enumerate(checkpoint.sliding):
                arrays[f"checkpoint.{number}.{slot}.keys"] = copy_array(keys)
                arrays[f"checkpoint.{number}.{slot}.values"] = copy_array(values)
        mx.eval(list(arrays.values()))
        copied = time.perf_counter() - started
        key = _key(tokens)
        with self.lock:
            self.entries[key] = len(tokens)
            self.pending = key
            if previous is not None:
                self.entries.pop(previous[0], None)
        self.writer = threading.Thread(
            target=self._write, args=(key, len(tokens), arrays, copied, previous), daemon=True
        )
        self.writer.start()

    def _write(self, key, length, arrays, copied, previous):
        started = time.perf_counter()
        path = self._path(key, length)
        temporary = path.with_name(path.stem + ".tmp.safetensors")
        try:
            # The arrays are evaluated copies, so writing them needs no GPU work on this thread. The file is made
            # 0600 first; MLX writes into it and keeps that mode.
            os.close(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
            mx.save_safetensors(str(temporary), arrays)
            temporary.chmod(0o600)
            temporary.replace(path)
        except Exception as error:
            logging.warning("Disk prompt cache: could not write %s: %s", path, error)
            with self.lock:
                self.entries.pop(key, None)
                if previous is not None:
                    self.entries[previous[0]] = previous[1]
            temporary.unlink(missing_ok=True)
            return
        if previous is not None:
            # The new file starts with the previous prompt and holds everything it did.
            self._path(*previous).unlink(missing_ok=True)
        logging.info(
            "Disk prompt cache: saved %d tokens (%.0f MiB) in %.2f s copy + %.2f s write",
            length, path.stat().st_size / 2**20, copied, time.perf_counter() - started,
        )
        self._evict(keep=path)

    def _evict(self, keep):
        # The other models' folders count too: one left by an older model or KV format would stay past the limit.
        # Only this cache's own file names (<length>-<key>.safetensors), never other files in a folder beside it.
        files = sorted(
            (p for p in self.directory.parent.glob("*/*.safetensors") if re.fullmatch(r"\d+-[0-9a-f]{32}", p.stem)),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= self.limit_bytes or path == keep:
                continue
            total -= path.stat().st_size
            if path.parent == self.directory:
                length, _, key = path.stem.partition("-")
                self._drop(key, length)
            else:
                path.unlink(missing_ok=True)

    def wait(self):
        if self.writer is not None:
            self.writer.join()
            self.writer = None

    def load(self, key, length, runtime):
        """The stored prompt as a PromptCache, or None; a file that cannot be loaded is deleted, so it never
        fails the next request too."""
        self.wait()
        with self.lock:
            if key not in self.entries:  # evicted by the write that just finished
                return None
        path = self._path(key, length)
        started = time.perf_counter()
        try:
            cache = self._load(path, key, length, runtime)
        except Exception as error:
            logging.warning("Disk prompt cache: could not load %s, deleting it: %s", path, error)
            self._drop(key, length)
            return None
        os.utime(path)
        logging.info("Disk prompt cache: loaded %d tokens in %.2f s", length, time.perf_counter() - started)
        return cache

    def _load(self, path, key, length, runtime):
        arrays = mx.load(str(path))
        tokens = arrays["tokens"].tolist()
        if len(tokens) != length or _key(tokens) != key:
            raise ValueError("the file does not hold the prompt its name says")
        storage = runtime.prefill_state(length, limit=length)["kv"]
        # Rebuild each bank from the saved runs in MLX: no host copies, one bank at a time, so a long prompt's
        # KV is held about twice while it loads, not four or five times.
        layout = list(_ranges(storage, length))
        sizes = [count * 128 for _, _, count in layout]
        saved = []
        while f"global.{len(saved)}" in arrays:
            saved.append(arrays.pop(f"global.{len(saved)}"))
        places = _places(sizes)
        held = [0] * len(saved)
        runs = []
        for (bank, first, _), size, (piece, offset) in zip(layout, sizes, places):
            if piece >= len(saved):
                raise ValueError(f"global KV has {len(saved)} pieces, the layout needs more")
            runs.append((bank, first, size, piece, offset))
            held[piece] += size
        if held != [piece.size for piece in saved]:
            raise ValueError(f"global KV holds {[p.size for p in saved]} values, the layout needs {held}")
        for index, bank in enumerate(storage.banks):
            if bank.size <= 1024:
                continue
            size, shape = bank.size, bank.shape
            storage.banks[index] = bank = None
            pieces, filled = [], 0
            for _, first, count, piece, offset in (run for run in runs if run[0] == index):
                if first > filled:
                    pieces.append(mx.zeros(first - filled, mx.bfloat16))
                pieces.append(saved[piece][offset : offset + count])
                filled = first + count
            if filled < size:
                pieces.append(mx.zeros(size - filled, mx.bfloat16))
            storage.banks[index] = mx.concatenate(pieces).reshape(shape)
            mx.eval(storage.banks[index])
        del saved
        layers = runtime.model.make_cache(length, storage)
        for index, layer in enumerate(layers):
            layer.offset = length
            if isinstance(layer, FixedSlidingKVCache):
                keys, values = arrays[f"sliding.{index}.keys"], arrays[f"sliding.{index}.values"]
                layer.capacity = keys.shape[-2]
                layer.key_buffers = (keys, mx.zeros_like(keys))
                layer.value_buffers = (values, mx.zeros_like(values))
                layer._valid, layer._ring_index, layer._slot = keys.shape[-2], 0, 0
        checkpoints = []
        number = 0
        while f"checkpoint.{number}.state" in arrays:
            state = arrays[f"checkpoint.{number}.state"].tolist()
            sliding = [
                (
                    arrays[f"checkpoint.{number}.{slot}.keys"],
                    arrays[f"checkpoint.{number}.{slot}.values"],
                    state[1 + slot * 2],
                    state[2 + slot * 2],
                )
                for slot in range((len(state) - 1) // 2)
            ]
            checkpoints.append(Checkpoint(state[0], sliding, arrays[f"checkpoint.{number}.logits"]))
            number += 1
        mx.eval([layer.state for layer in layers if isinstance(layer, FixedSlidingKVCache)])
        return PromptCache(tokens, layers, arrays["logits"], storage, checkpoints)
