"""in_assigned_scope must fail closed and use POSIX path-segment matching.

Empty or missing assigned_files allows nothing. A scope entry matches a path
only as an exact file, or as a directory prefix followed by '/'. Paths with
'..', an absolute form, or a backslash are rejected, matching the snapshot_path
text rules in contract/v1/project_state.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)


class AssignedScope(unittest.TestCase):
    def setUp(self):
        self.fn = L.in_assigned_scope

    def test_empty_or_missing_scope_allows_nothing(self):
        for scope in (None, [], (), ""):
            with self.subTest(scope=scope):
                self.assertFalse(self.fn("src/a.py", scope))
                self.assertFalse(self.fn("out.txt", scope))

    def test_exact_file_match(self):
        self.assertTrue(self.fn("src/a.py", ["src/a.py"]))
        self.assertFalse(self.fn("src/b.py", ["src/a.py"]))
        self.assertFalse(self.fn("src/a.py.bak", ["src/a.py"]))

    def test_directory_prefix_uses_path_boundary(self):
        self.assertTrue(self.fn("src/a/x.py", ["src/a"]))
        self.assertTrue(self.fn("src/a/x.py", ["src/a/"]))
        self.assertTrue(self.fn("src/style.css", ["src/"]))
        self.assertTrue(self.fn("src/style.css", ["src"]))
        # 'src/a' must not match 'src/ab' (the line-960 startswith bug).
        self.assertFalse(self.fn("src/ab", ["src/a"]))
        self.assertFalse(self.fn("src/ab.py", ["src/a"]))
        self.assertFalse(self.fn("shopping.py", ["shop"]))

    def test_rejects_dotdot_absolute_and_backslash(self):
        self.assertFalse(self.fn("../secret", ["../secret"]))
        self.assertFalse(self.fn("foo/../bar.py", ["foo/../bar.py"]))
        self.assertFalse(self.fn("/etc/passwd", ["/etc/passwd"]))
        self.assertFalse(self.fn("src/a.py", ["/src/a.py"]))
        self.assertFalse(self.fn("src\\a.py", ["src\\a.py"]))
        self.assertFalse(self.fn("src/a.py", ["src\\a.py"]))
        self.assertFalse(self.fn("C:/Windows/x.py", ["C:/Windows/x.py"]))
        self.assertFalse(self.fn("foo/./bar.py", ["foo/./bar.py"]))
        self.assertFalse(self.fn("src//a.py", ["src//a.py"]))

    def test_invalid_scope_entry_does_not_grant_access(self):
        self.assertFalse(self.fn("src/a.py", ["..", "", "/abs", "src\\a.py"]))
        # A valid entry still matches when mixed with junk.
        self.assertTrue(self.fn("src/a.py", ["..", "src/a.py", "/abs"]))


class ManifestScope(unittest.TestCase):
    """The _manifest artifact path (launcher.py around the assigned_files check)."""

    def _manifest(self, assigned, artifacts):
        tmp = tempfile.mkdtemp(prefix="hyvv-scope-")
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp, "backoff_s": [0.2],
               "projects": {}, "model": "deepseek-v4.1-flash", "base_url": "http://x", "wire": "openai",
               "image": "cua-bee:test", "runtime": "runc", "memory_mb": 512}
        la = L.Launcher(cfg, L.FakeBackend(cfg))
        brief = {"objective": "x"}
        if assigned is not None:
            brief["assigned_files"] = assigned
        a = {"attempt_id": "att_scope00000001", "brief": brief, "kind": "opencode", "mode": None, "tier": None}
        res = {"status": "completed", "text": "done", "artifacts": artifacts, "tool_calls": 1}

        async def go():
            return await la._manifest(a, res)

        return asyncio.run(go())

    def test_empty_scope_marks_artifacts_out_of_scope(self):
        for assigned in ([], None):
            with self.subTest(assigned=assigned):
                man = self._manifest(assigned, [{"path": "out.txt", "size": 2}])
                rows = man["observed"]["artifacts"]
                self.assertEqual(len(rows), 1)
                self.assertFalse(rows[0]["in_assigned_scope"])

    def test_prefix_does_not_match_a_longer_sibling(self):
        man = self._manifest(["src/a"], [{"path": "src/ab", "size": 2}, {"path": "src/a/x.py", "size": 2}])
        by = {r["path"]: r["in_assigned_scope"] for r in man["observed"]["artifacts"]}
        self.assertFalse(by["src/ab"])
        self.assertTrue(by["src/a/x.py"])

    def test_exact_file_is_in_scope(self):
        man = self._manifest(["out.txt"], [{"path": "out.txt", "size": 2}, {"path": "other.txt", "size": 1}])
        by = {r["path"]: r["in_assigned_scope"] for r in man["observed"]["artifacts"]}
        self.assertTrue(by["out.txt"])
        self.assertFalse(by["other.txt"])


class PatchScope(unittest.TestCase):
    """CuaBackend.patch is the other in_assigned_scope call site."""

    def _files(self, assigned, name_status="M\tsrc/style.css\nM\tsrc/ab.py\n"):
        be = L.CuaBackend.__new__(L.CuaBackend)

        async def sh(sb, line, timeout_ms=120000):
            if "name-status" in line:
                return name_status, "", 0
            if "diff --cached --binary" in line:
                return "diff --git a/x b/x\n", "", 0
            if "sha256sum" in line:
                return "ab" * 32, "", 0
            return "", "", 0

        async def sandbox(name):
            return "sb"

        be._sh = sh
        be._sandbox = sandbox
        brief = {"objective": "x"}
        if assigned is not None:
            brief["assigned_files"] = assigned
        a = {"attempt_id": "att_scope00000002", "kind": "opencode", "sandbox": "s",
             "workdir": "/tmp/w", "brief": brief}
        return asyncio.run(be.patch(a))["files"]

    def test_empty_scope_marks_patch_files_out_of_scope(self):
        for assigned in ([], None):
            with self.subTest(assigned=assigned):
                files = self._files(assigned, "M\tsrc/style.css\n")
                self.assertEqual(files[0]["path"], "src/style.css")
                self.assertFalse(files[0]["in_assigned_scope"])

    def test_directory_scope_and_boundary(self):
        files = self._files(["src/a"], "M\tsrc/a/x.py\nM\tsrc/ab.py\n")
        by = {f["path"]: f["in_assigned_scope"] for f in files}
        self.assertTrue(by["src/a/x.py"])
        self.assertFalse(by["src/ab.py"])

    def test_src_slash_still_covers_children(self):
        files = self._files(["src/"], "M\tsrc/style.css\n")
        self.assertTrue(files[0]["in_assigned_scope"])


if __name__ == "__main__":
    unittest.main()
