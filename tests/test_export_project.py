"""0.1.14: export_project must not fetch when source_rev is already local, or when origin is missing,
and must not fail if the brief's work branch is not on the remote yet."""
import importlib.util, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher"] = L
spec.loader.exec_module(L)


def _run(args, cwd=None, check=True):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _init_repo(path, text="v1\n"):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main", str(path)])
    _run(["git", "-C", str(path), "config", "user.email", "t@t"])
    _run(["git", "-C", str(path), "config", "user.name", "t"])
    (path / "f.txt").write_text(text)
    _run(["git", "-C", str(path), "add", "f.txt"])
    _run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"])
    rev = _run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()
    return path, rev


class ExportProjectSourceRev(unittest.TestCase):
    def test_version(self):
        self.assertEqual(L.VERSION, "0.1.16")

    def test_no_origin_local_source_rev_new_work_branch_succeeds(self):
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            repo, rev = _init_repo(Path(d) / "repo")
            got, tar = L.export_project(repo, source_rev=rev, branch="bee/pilot-new")
            self.assertEqual(got, rev)
            self.assertTrue(tar.startswith(b"./.") or b"f.txt" in tar or len(tar) > 0)

    def test_origin_present_but_work_branch_missing_still_succeeds_when_rev_is_local(self):
        # 0.1.13 would git fetch the missing work branch with check=True and fail, even
        # though source_rev was already a local commit.
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            d = Path(d)
            up, rev = _init_repo(d / "up")
            pc = d / "pc"
            _run(["git", "clone", "-q", str(up), str(pc)])
            got, tar = L.export_project(pc, source_rev=rev, branch="bee/not-pushed-yet")
            self.assertEqual(got, rev)
            self.assertGreater(len(tar), 0)
            # checkout was not moved
            head = _run(["git", "-C", str(pc), "rev-parse", "HEAD"]).stdout.strip()
            self.assertEqual(head, rev)

    def test_local_source_rev_skips_fetch_even_if_origin_is_unreachable(self):
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            repo, rev = _init_repo(Path(d) / "repo")
            _run(["git", "-C", str(repo), "remote", "add", "origin", str(Path(d) / "no-such-origin.git")])
            got, tar = L.export_project(repo, source_rev=rev, branch="bee/not-pushed-yet")
            self.assertEqual(got, rev)
            self.assertGreater(len(tar), 0)

    def test_missing_source_rev_without_origin_fails_clearly(self):
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            repo, _rev = _init_repo(Path(d) / "repo")
            missing = "ab" * 20
            with self.assertRaises(RuntimeError) as ctx:
                L.export_project(repo, source_rev=missing, branch="bee/x")
            self.assertIn("no origin remote", str(ctx.exception))

    def test_fetches_missing_local_rev_from_origin_when_needed(self):
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            d = Path(d)
            up, _ = _init_repo(d / "up", "v1\n")
            pc = d / "pc"
            _run(["git", "clone", "-q", str(up), str(pc)])
            (up / "f.txt").write_text("v2\n")
            _run(["git", "-C", str(up), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "2"])
            new = _run(["git", "-C", str(up), "rev-parse", "HEAD"]).stdout.strip()
            self.assertNotEqual(new, _run(["git", "-C", str(pc), "rev-parse", "HEAD"]).stdout.strip())
            got, tar = L.export_project(pc, source_rev=new, branch="bee/not-on-remote")
            self.assertEqual(got, new)
            self.assertGreater(len(tar), 0)

    def test_bad_source_rev_still_rejected(self):
        with tempfile.TemporaryDirectory(prefix="exp14-") as d:
            repo, _ = _init_repo(Path(d) / "repo")
            with self.assertRaises(ValueError):
                L.export_project(repo, source_rev="not-a-sha", branch="bee/x")


if __name__ == "__main__":
    unittest.main()
