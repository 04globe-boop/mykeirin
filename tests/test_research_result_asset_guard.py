import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "logger"))
import result_asset_guard as guard


class ResultAssetGuardTest(unittest.TestCase):
    @staticmethod
    def blob(value):
        return gzip.compress((json.dumps(value) + "\n").encode())

    @staticmethod
    def identity(raw, asset_id=100):
        return {"id": asset_id, "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}

    def test_failed_restore_has_no_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(guard, "lookup", return_value=(10, self.identity(self.blob({"a": 1})))), \
                 patch.object(guard, "gh", side_effect=RuntimeError("read failed")):
                with self.assertRaises(RuntimeError):
                    guard.restore("extra_202611.jsonl.gz", root)
            self.assertFalse((root / "restore.json").exists())

    def test_wrong_restore_content_has_no_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(guard, "lookup", return_value=(10, self.identity(self.blob({"a": 1})))), \
                 patch.object(guard, "gh", return_value=self.blob({"a": 2})):
                with self.assertRaises(RuntimeError):
                    guard.restore("extra_202611.jsonl.gz", root)
            self.assertFalse((root / "restore.json").exists())

    def test_verify_requires_after_original_append_and_current_identity(self):
        name = "extra_202611.jsonl.gz"
        raw = self.blob({"a": 1})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = self.identity(raw)
            with patch.object(guard, "lookup", return_value=(10, asset)), patch.object(guard, "gh", return_value=raw):
                guard.restore(name, root)
                (root / name).write_bytes(raw + self.blob({"b": 2}))
                self.assertEqual(guard.verify(name, root), "replace")
                (root / name).write_bytes(self.blob({"b": 2}))
                with self.assertRaises(RuntimeError):
                    guard.verify(name, root)
                (root / name).write_bytes(raw)
                self.assertEqual(guard.verify(name, root), "unchanged")
            with patch.object(guard, "lookup", return_value=(10, self.identity(raw, 101))):
                with self.assertRaises(RuntimeError):
                    guard.verify(name, root)

    def test_confirmed_absence_uses_create_and_detects_conflict(self):
        name = "extra_202611.jsonl.gz"
        raw = self.blob({"a": 1})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(guard, "lookup", return_value=(10, None)):
                guard.restore(name, root)
                self.assertEqual(guard.verify(name, root), "unchanged")
                (root / name).write_bytes(raw)
                self.assertEqual(guard.verify(name, root), "create")
            with patch.object(guard, "lookup", return_value=(10, self.identity(raw))):
                with self.assertRaises(RuntimeError):
                    guard.verify(name, root)


if __name__ == "__main__":
    unittest.main()
