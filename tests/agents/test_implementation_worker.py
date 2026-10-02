"""Validate worker artifacts before any write; no model or app required."""
from pathlib import Path
import tempfile
import unittest
from scripts import claude_worker as worker


class ImplementationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = {"allowed_changes": ["src/example.py"]}

    def test_out_of_scope_change_rejects_entire_batch(self):
        result = {"status": "completed", "changes": [
            {"path": "src/example.py", "content": "good = True\n"},
            {"path": "src/other.py", "content": "bad = True\n"}]}
        with self.assertRaises(worker.WorkerError):
            worker.stage_changes(self.root, self.task, result)
        self.assertFalse((self.root / "src/example.py").exists())

    def test_paths_cannot_escape_or_target_settings(self):
        for name in ["../src/a.py", "src/../a.py", "src//a.py", "C:/src/a.py",
                     "src/.env", "scripts/run.py", "src/data/value.json", "src/a.py/other"]:
            with self.subTest(name=name), self.assertRaises(worker.WorkerError):
                worker.write_path(self.root, name)

    def test_only_worktree_file_written_and_hashed(self):
        tree = self.root / "worktree"
        result = {"status": "completed", "changes": [{"path": "src/example.py", "content": "value = 42\n"}]}
        changes = worker.stage_changes(tree, self.task, result)
        self.assertEqual((tree / "src/example.py").read_text(), "value = 42\n")
        self.assertFalse((self.root / "src/example.py").exists())
        self.assertEqual(changes[0]["sha256"], worker.digest(b"value = 42\n"))

    def test_blocked_or_duplicate_result_cannot_write(self):
        change = {"path": "src/example.py", "content": "value = 42\n"}
        for result in [{"status":"blocked", "changes":[change]},
                       {"status":"completed", "changes":[change,change]}]:
            with self.assertRaises(worker.WorkerError):
                worker.stage_changes(self.root, self.task, result)
        self.assertFalse((self.root / "src").exists())

    def test_symlink_target_refused(self):
        outside = self.root / "outside.py"
        outside.write_text("original")
        (self.root / "src").mkdir()
        try:
            (self.root / "src/example.py").symlink_to(outside)
        except OSError:
            self.skipTest("Symlink permission unavailable")
        with self.assertRaises(worker.WorkerError):
            worker.stage_changes(self.root, self.task, {"status":"completed", "changes":[
                {"path":"src/example.py", "content":"changed"}]})
        self.assertEqual(outside.read_text(), "original")
