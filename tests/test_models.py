import json
import os
from pathlib import Path
import platform
import shutil
import tempfile
import unittest
from unittest.mock import patch

from flint.core import Cancelled, MODEL_FILES, Problem, REC_SHA, clone_file
from test_flint import Fixture


class ModelReuseTests(Fixture):
    def source(self):
        model = Path.home() / ".local/share/rec/models/flint"
        model.mkdir(parents=True)
        for name in MODEL_FILES:
            (model / name).write_text(name)
        (model / "flint-int4.json").write_text(json.dumps({
            "checkpoint_files": {name: "0" * 64 for name in MODEL_FILES}}))
        (model / "verified.json").write_text('{"private": "original inode metadata"}')
        return model

    def test_clone_preserves_source_and_does_not_import_verification_or_other_files(self):
        source = self.source()
        (source / "unrelated-secret").write_text("do not copy")
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
        with patch("flint.core.clone_file", side_effect=shutil.copyfile) as clone:
            self.assertTrue(self.manager.reuse_models())
        target = self.manager.home / "rec/models/flint"
        self.assertEqual({p.name for p in target.iterdir()}, MODEL_FILES | {"flint-int4.json"})
        self.assertEqual(clone.call_count, len(MODEL_FILES) + 1)
        self.assertFalse(self.manager.models_verified())
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()})
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["flint"])

    def test_absent_source_downloads_and_existing_partial_is_not_overwritten(self):
        with patch("flint.core.clone_file") as clone:
            self.assertFalse(self.manager.reuse_models())
            self.source()
            target = self.manager.home / "rec/models/flint"
            target.mkdir(parents=True)
            partial = target / "model-00000.safetensors.part"
            partial.write_bytes(b"partial download")
            self.assertFalse(self.manager.reuse_models())
            self.assertEqual(partial.read_bytes(), b"partial download")
            clone.assert_not_called()

    def test_malformed_incomplete_symlink_and_ambiguous_sources_are_rejected(self):
        source = self.source()
        manifest = source / "flint-int4.json"
        original = manifest.read_bytes()
        for malformed in ("{}", "null", "broken", '{"checkpoint_files": {"../escape": "hash"}}'):
            manifest.write_text(malformed)
            with self.subTest(malformed=malformed), self.assertRaisesRegex(Problem, "메타데이터"):
                self.manager.reuse_models()
        manifest.write_bytes(original)
        config = source / "config.json"
        config.unlink()
        with self.assertRaisesRegex(Problem, "불완전"):
            self.manager.reuse_models()
        config.symlink_to(manifest)
        with self.assertRaisesRegex(Problem, "심볼릭"):
            self.manager.reuse_models()
        config.unlink()
        config.write_text("{}")
        extra = source.parent / "other"
        extra.mkdir()
        (extra / manifest.name).write_bytes(original)
        with self.assertRaisesRegex(Problem, "여러 개"):
            self.manager.reuse_models()

    def test_failed_or_cancelled_clone_is_cleaned_and_does_not_fall_back(self):
        source = self.source()
        for cancel in (False, True):
            def fail_after_one(src, dst):
                shutil.copyfile(src, dst)
                if cancel:
                    self.manager.cancel.set()
                else:
                    raise OSError("cross-device clone")
            with self.subTest(cancel=cancel), \
                    patch("flint.core.clone_file", side_effect=fail_after_one), \
                    patch.object(self.manager, "run") as run:
                with self.assertRaises(Cancelled if cancel else Problem):
                    self.manager.reuse_models()
                self.assertEqual(list((self.manager.home / "rec/models").iterdir()), [])
                self.assertTrue((source / "verified.json").exists())
                run.assert_not_called()
            self.manager.cancel.clear()

    def test_pull_verifies_import_instead_of_downloading(self):
        m = self.manager
        rec = m.home / "bin/rec"
        rec.parent.mkdir()
        rec.write_text("mock")
        self.source()
        with patch("flint.core.digest", return_value=REC_SHA), \
                patch("flint.core.clone_file", side_effect=shutil.copyfile), \
                patch.object(m, "run") as run, patch.object(m, "models_verified", return_value=True):
            m.model_command("pull")
        run.assert_called_once_with([rec, "doctor"], timeout=21600)

    def test_install_clones_before_installer_disk_check_then_verifies(self):
        m = self.manager
        self.source()
        commands = []
        def run(args, **kwargs):
            commands.append([str(a) for a in args])
            if str(args[0]) == "/usr/bin/curl":
                Path(args[args.index("-o") + 1]).write_bytes(b"mock rec")
            if str(args[-1]).endswith("install.py"):
                self.assertTrue((m.home / "rec/models/flint/config.json").exists())
        with patch("platform.system", return_value="Darwin"), \
                patch("platform.machine", return_value="arm64"), \
                patch("flint.core.digest", return_value=REC_SHA), \
                patch("flint.core.clone_file", side_effect=shutil.copyfile), \
                patch.object(m, "run", side_effect=run), \
                patch.object(m, "models_verified", return_value=True):
            m.install()
        self.assertEqual(commands[-1], [str(m.home / "bin/rec"), "doctor"])

    def test_destination_symlink_is_not_followed(self):
        self.source()
        victim = self.manager.home / "victim"
        victim.mkdir()
        (self.manager.home / "rec/models").symlink_to(victim)
        with self.assertRaisesRegex(Problem, "심볼릭"):
            self.manager.reuse_models()
        self.assertEqual(list(victim.iterdir()), [])


@unittest.skipUnless(platform.system() == "Darwin", "macOS clonefile API")
class NativeCloneTests(unittest.TestCase):
    def test_real_clone_is_independent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source", Path(directory) / "destination"
            source.write_bytes(b"original model fixture")
            clone_file(source, destination)
            self.assertNotEqual(source.stat().st_ino, destination.stat().st_ino)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            destination.write_bytes(b"changed")
            self.assertEqual(source.read_bytes(), b"original model fixture")
            with self.assertRaises(FileExistsError):
                clone_file(source, destination)
            self.assertEqual(destination.read_bytes(), b"changed")
