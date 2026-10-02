"""Isolated orchestration tests: no application, network or real model calls."""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import claude_worker as worker


RESULT = {"status": "completed", "summary": "Reviewed the selected contract.",
          "findings": [], "checks": ["Read doc.md"], "limitations": ["No tests were executed."]}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="worker-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "doc.md").write_text("# Public contract\nA small scoped example.\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("Read only. No recursion. Return to Lead.", encoding="utf-8")
        self.task = {"id": "unit-review", "role": "researcher", "mode": "read_only", "complexity": "medium",
                     "goal": "Review this contract", "context": ["Public fixture only"], "relevant_paths": ["doc.md"],
                     "constraints": ["Read only"], "acceptance_criteria": ["Cite evidence"], "allowed_changes": [],
                     "forbidden_changes": ["All files"], "delegation_reason": "Independent contract review",
                     "context_cost_reason": "One small file suffices"}
        self.state = self.root / ".agent-runs"

    def save_task(self, task=None):
        path = self.root / "task.json"
        path.write_text(json.dumps(task or self.task), encoding="utf-8")
        return path

    @contextmanager
    def fake_cli(self, implementation=None):
        answer = {"result": copy.deepcopy(RESULT), "usage": {}, "reported_cost_usd": 0.01, "models": ["fake"]}
        with patch.object(worker, "find_cli", return_value="fake-claude"), \
             patch.object(worker, "check_cli", return_value={"version": "fake"}), \
             patch.object(worker, "invoke", side_effect=implementation, return_value=answer) as invoke:
            yield invoke

    def test_contract_rejects_missing_scope_and_implementation_roles(self):
        mutations = [("allowed_changes", ["src/"]), ("mode", "implementation"),
                     ("role", "backend_implementation"), ("role", "frontend_implementation"),
                     ("complexity", "trivial"), ("acceptance_criteria", [])]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                changed = {**self.task, key: value}
                with self.assertRaises(worker.WorkerError):
                    worker.load_task(self.save_task(changed))
        changed = dict(self.task)
        del changed["forbidden_changes"]
        with self.assertRaises(worker.WorkerError):
            worker.load_task(self.save_task(changed))

    def test_path_boundary_private_files_binary_and_large_context(self):
        (self.root / ".env").write_text("PRIVATE=do-not-read", encoding="utf-8")
        (self.root / "blob.txt").write_bytes(b"binary\0value")
        (self.root / "large.md").write_text("x" * (worker.MAX_FILE_BYTES + 1), encoding="utf-8")
        for name in ("../outside.md", ".", ".env", ".claude/settings.local.json",
                     "data/customer.md", "secrets.key", "C:/private.txt", "blob.txt", "large.md"):
            with self.subTest(path=name), self.assertRaises(worker.WorkerError):
                worker.snapshot(self.root, {**self.task, "relevant_paths": [name]})

    def test_directory_manifest_is_bounded_and_exact(self):
        folder = self.root / "docs"
        folder.mkdir()
        for i in range(worker.MAX_FILES + 1):
            (folder / f"{i}.md").write_text("safe", encoding="utf-8")
        with self.assertRaisesRegex(worker.WorkerError, "Too many files"):
            worker.snapshot(self.root, {**self.task, "relevant_paths": ["docs"]})
        files = worker.snapshot(self.root, self.task)
        self.assertEqual([f["path"] for f in files], ["doc.md"])
        self.assertNotIn("content", worker.manifest(files)[0])

    def test_snapshot_normalizes_root_before_relative_path_checks(self):
        folder = self.root / "docs"
        folder.mkdir()
        (folder / "review.md").write_text("Public review context", encoding="utf-8")
        unresolved_root = folder / ".."
        for selected, expected in (("doc.md", "doc.md"), ("docs", "docs/review.md")):
            with self.subTest(selected=selected):
                task = {**self.task, "relevant_paths": [selected]}
                files = worker.snapshot(unresolved_root, task)
                self.assertEqual([item["path"] for item in files], [expected])
                self.assertEqual(files, worker.snapshot(self.root.resolve(), task))

    def test_dockerfile_is_allowed_but_other_extensionless_files_are_not(self):
        (self.root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
        (self.root / "private-data").write_text("not a source filename", encoding="utf-8")
        files = worker.snapshot(self.root, {**self.task, "relevant_paths": ["Dockerfile"]})
        self.assertEqual([f["path"] for f in files], ["Dockerfile"])
        with self.assertRaises(worker.WorkerError):
            worker.snapshot(self.root, {**self.task, "relevant_paths": ["private-data"]})

    def test_secret_in_task_or_source_never_reaches_cli(self):
        secret = "sk-" + "A" * 30
        with self.assertRaises(worker.WorkerError):
            worker.load_task(self.save_task({**self.task, "context": [secret]}))
        (self.root / "doc.md").write_text(secret, encoding="utf-8")
        with self.fake_cli() as invoke, self.assertRaises(worker.WorkerError):
            worker.run_task(self.root, self.task)
        invoke.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertNotIn(secret, worker.brief(secret))

    def test_ignored_files_are_rejected_even_with_safe_names(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        (self.root / ".gitignore").write_text("doc.md\n", encoding="utf-8")
        with self.assertRaisesRegex(worker.WorkerError, "ignored"):
            worker.snapshot(self.root, self.task)

    def test_hardlinks_are_rejected(self):
        try:
            os.link(self.root / "doc.md", self.root / "alias.md")
        except OSError:
            self.skipTest("Hardlinks unavailable")
        with self.assertRaisesRegex(worker.WorkerError, "hardlinked"):
            worker.snapshot(self.root, self.task)

    def test_cli_missing_never_installs_anything(self):
        with patch.object(worker.shutil, "which", return_value=None), \
             patch.object(worker.subprocess, "run") as run, self.assertRaises(worker.WorkerError):
            worker.find_cli()
        run.assert_not_called()

    def test_npm_native_executable_avoids_windows_shell_quoting(self):
        shim = self.root / "claude.cmd"
        native = self.root / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"fixture")
        with patch.object(worker.shutil, "which", side_effect=lambda name: str(shim) if name == "claude" else None):
            self.assertEqual(worker.find_cli(), str(native))

    def test_command_has_no_tools_customizations_persistence_or_shell_bypass(self):
        args = worker.command("claude", 1.0)
        self.assertEqual(args[args.index("--tools") + 1], "")
        for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--print"):
            self.assertIn(flag, args)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

    def test_invocation_passes_context_only_on_stdin_in_temp_directory(self):
        envelope = {"subtype": "success", "is_error": False, "result": json.dumps(RESULT)}
        class FakeProcess:
            returncode = 0
            def __init__(instance, args, **kwargs):
                self.assertNotIn("PRIVATE_PROMPT", args)
                self.assertNotEqual(Path(kwargs["cwd"]), self.root)
                self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
                self.assertEqual(kwargs["env"]["SPOOLDESK_WORKER"], "1")
                self.assertEqual(kwargs["env"]["DISABLE_UPDATES"], "1")
                self.assertFalse(kwargs.get("shell", False))
            def communicate(instance, text, timeout):
                self.assertEqual(text, "PRIVATE_PROMPT")
                return json.dumps(envelope), "diagnostic that is not retained"
        with patch.object(worker.subprocess, "Popen", FakeProcess):
            self.assertEqual(worker.invoke("fake", "PRIVATE_PROMPT", 30, 1)["result"], RESULT)

    def test_default_limit_is_one_and_lock_is_released_after_exception(self):
        with worker.reserve(self.state, self.task, False, "", []):
            with self.assertRaisesRegex(worker.WorkerError, "limit"):
                with worker.reserve(self.state, {**self.task, "id": "other-task"}, False, "", []):
                    self.fail("second worker admitted")
        with worker.reserve(self.state, {**self.task, "id": "after-exit"}, False, "", []) as attempt:
            self.assertEqual(attempt, 1)

    def large(self, task_id, stream="backend", group="release"):
        return {**self.task, "id": task_id, "complexity": "large", "parallel_group": group,
                "workstream": stream, "parallel_reason": "Independent contracts; no shared writes"}

    def test_two_independent_large_workers_allowed_but_third_rejected(self):
        with worker.reserve(self.state, self.large("large-one"), True, "", []):
            with worker.reserve(self.state, self.large("large-two", "frontend"), True, "", []):
                with self.assertRaisesRegex(worker.WorkerError, "limit"):
                    with worker.reserve(self.state, self.large("large-three", "qa"), True, "", []):
                        self.fail("third worker admitted")

    def test_parallel_cannot_mix_groups_duplicate_streams_or_medium_tasks(self):
        with self.assertRaises(worker.WorkerError):
            with worker.reserve(self.state, self.task, True, "", []):
                self.fail("medium task admitted")
        with worker.reserve(self.state, self.large("large-one"), True, "", []):
            for task in (self.large("large-two"), self.large("large-three", "frontend", "other")):
                with self.assertRaises(worker.WorkerError):
                    with worker.reserve(self.state, task, True, "", []):
                        self.fail("dependent worker admitted")

    def test_os_lock_survives_other_process_probe_and_releases_on_exit(self):
        self.state.mkdir()
        path = self.state / "process.lock"
        lock = worker.FileLock(path)
        self.assertTrue(lock.acquire())
        code = ("from pathlib import Path; from scripts.claude_worker import FileLock; "
                "import sys; lock=FileLock(Path(sys.argv[1])); "
                "print(int(lock.acquire())); lock.close()")
        try:
            probe = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True)
            self.assertEqual(probe.stdout.strip(), "0")
        finally:
            lock.close()
        probe = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), "1")

    def test_repeat_requires_reason_and_is_counted_without_full_prompts(self):
        with self.fake_cli() as invoke:
            first = worker.run_task(self.root, self.task)
            self.assertEqual(first["acceptance"], "pending_lead_review")
            with self.assertRaisesRegex(worker.WorkerError, "retry-reason"):
                worker.run_task(self.root, self.task)
            second = worker.run_task(self.root, self.task, retry_reason="Additional evidence needs confirmation")
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(second["repeat_count"], 1)
        ledger = (self.state / "delegations.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("A small scoped example", ledger)
        self.assertNotIn("Public fixture only", ledger)
        self.assertNotIn("No tests were executed", ledger)

    def test_worker_result_needs_explicit_lead_acceptance(self):
        with self.fake_cli():
            worker.run_task(self.root, self.task)
        self.assertFalse(any(e["event"] == "lead_review" for e in worker.events(self.state)))
        worker.accept(self.state, self.task["id"], 1, "accepted", "Checked cited source; fixture tests passed.")
        self.assertEqual(worker.events(self.state)[-1]["decision"], "accepted")
        with self.assertRaises(worker.WorkerError):
            worker.accept(self.state, self.task["id"], 1, "accepted", "Duplicate acceptance")

    def test_failed_invocation_is_logged_once_without_sensitive_diagnostics(self):
        def fail(*args):
            raise RuntimeError("private-provider-diagnostic")
        with self.fake_cli(fail) as invoke, self.assertRaises(RuntimeError):
            worker.run_task(self.root, self.task)
        self.assertEqual(invoke.call_count, 1)
        rows = worker.events(self.state)
        self.assertEqual([r["event"] for r in rows], ["started", "failed"])
        self.assertNotIn("private-provider-diagnostic", json.dumps(rows))

    def test_source_changes_during_worker_are_not_accepted(self):
        def change(*args):
            (self.root / "doc.md").write_text("Changed by another process", encoding="utf-8")
            return {"result": RESULT, "usage": {}, "reported_cost_usd": None, "models": []}
        with self.fake_cli(change), self.assertRaisesRegex(worker.WorkerError, "changed"):
            worker.run_task(self.root, self.task)
        self.assertEqual(worker.events(self.state)[-1]["event"], "failed")

    def test_recursion_is_blocked_before_cli_or_registry(self):
        with patch.dict(os.environ, {"SPOOLDESK_WORKER": "1"}), self.fake_cli() as invoke, \
             self.assertRaisesRegex(worker.WorkerError, "Recursive"):
            worker.run_task(self.root, self.task)
        invoke.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_malformed_response_is_not_an_accepted_result(self):
        for value in (None, [], {"status": "completed"}, {**RESULT, "checks": "trust me"},
                      {**RESULT, "findings": [{"detail": "missing citation"}]}):
            with self.subTest(value=value), self.assertRaises(worker.WorkerError):
                worker.validate_result(value)

    def test_timeout_terminates_only_created_process_and_does_not_retry(self):
        class TimedOutProcess:
            pid = 424242
            calls = 0
            def communicate(instance, text=None, timeout=None):
                instance.calls += 1
                if instance.calls == 1:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return "", ""
        process = TimedOutProcess()
        with patch.object(worker.subprocess, "Popen", return_value=process) as launch, \
             patch.object(worker.subprocess, "run") as stop_windows, \
             patch.object(worker.os, "killpg", create=True) as stop_posix, \
             self.assertRaisesRegex(worker.WorkerError, "timeout"):
            worker.invoke("fake", "task", 10, 1)
        launch.assert_called_once()
        if os.name == "nt":
            self.assertEqual(stop_windows.call_args.args[0], ["taskkill", "/PID", "424242", "/T", "/F"])
        else:
            stop_posix.assert_called_once_with(424242, worker.signal.SIGKILL)

    def test_old_cli_missing_isolation_flags_is_refused(self):
        old = subprocess.CompletedProcess([], 0, stdout="old CLI without required flags", stderr="")
        with patch.object(worker.subprocess, "run", return_value=old), \
             self.assertRaisesRegex(worker.WorkerError, "isolation flags"):
            worker.check_cli("fake")


if __name__ == "__main__":
    unittest.main()
