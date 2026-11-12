"""Keep the four status labels honest.

* openapi.yaml operations, STATUS.json and the README tables agree.
* `launcher-0.1.15` routes answer on an in-process launcher; every other route
  is absent from it (so nothing unshipped is presented as shipped, and nothing
  shipped is left labelled unshipped).
* `reference` items name reference code and a test class that exist.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from contract.v1 import status as S  # noqa: E402

LABELS = set(S.STATUS["labels"])


def openapi_ops() -> dict[str, str]:
    ops, path, method = {}, None, None
    for line in (ROOT / "contract/v1/openapi.yaml").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^  (/v1\S*):\s*$", line)
        if m:
            path, method = m.group(1), None
            continue
        m = re.match(r"^    (get|post|put|delete|patch):\s*$", line)
        if m and path:
            method = m.group(1).upper()
            ops.setdefault(f"{method} {path}", None)
            continue
        m = re.match(r"^      x-hyvv-status:\s*(\S+)\s*$", line)
        if m and path and method:
            ops[f"{method} {path}"] = m.group(1)
    return ops


class Labels(unittest.TestCase):
    def test_every_label_is_one_of_four(self):
        self.assertEqual(LABELS, {"launcher-0.1.15", "reference", "specified", "proposed"})
        for k, v in {**S.STATUS["endpoints"], **S.STATUS["features"]}.items():
            self.assertIn(v, LABELS, k)

    def test_version_is_labelled_implemented_in_the_launcher(self):
        self.assertEqual(S.STATUS["endpoints"]["GET /v1/version"], "launcher-0.1.15")
        self.assertEqual(S.STATUS["features"]["GET /v1/version capabilities list"], "launcher-0.1.15")

    def test_openapi_matches_status_json(self):
        ops = openapi_ops()
        self.assertEqual(ops, S.STATUS["endpoints"])
        text = (ROOT / "contract/v1/openapi.yaml").read_text(encoding="utf-8")
        self.assertNotIn("x-implementation:", text)

    def test_readme_tables_are_current(self):
        for p in S.DOCS:
            cur = S.current_block(p.read_text(encoding="utf-8"))
            self.assertIsNotNone(cur, f"{p.name} lacks the status block")
            self.assertEqual(cur, S.block(), f"{p.relative_to(ROOT)} is stale; run python -m contract.v1.status --write")

    def test_unshipped_items_are_not_described_as_shipped(self):
        bad = re.compile(r"(?i)(already implemented|ships today|is implemented in launcher)[^\n]*(/artifact|/v1/leases|project_parked)")
        for p in [ROOT / "README.md", ROOT / "contract/v1/README.md", ROOT / "contract/v1/LEASES.md", ROOT / "docs/CI.md"]:
            self.assertIsNone(bad.search(p.read_text(encoding="utf-8")), p.name)

    def test_reference_items_have_code_and_tests(self):
        from contract.v1 import reference
        tests = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "contract/v1/tests").glob("test_*.py"))
        ref_items = [k for k, v in {**S.STATUS["endpoints"], **S.STATUS["features"]}.items() if v == "reference"]
        self.assertTrue(ref_items)
        for item in ref_items:
            ev = S.STATUS["reference_evidence"].get(item)
            self.assertTrue(ev, f"no evidence listed for {item}")
            cls, meth = ev[0].split(".")
            self.assertTrue(hasattr(getattr(reference, cls), meth), ev[0])
            self.assertIn(f"class {ev[1]}(", tests, ev[1])


class LauncherRoutes(unittest.TestCase):
    """Probe the real launcher.py (fake backend) on loopback."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("hyvv_launcher_status", ROOT / "launcher.py")
        L = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(L)
        from http.server import ThreadingHTTPServer
        tmp = tempfile.mkdtemp(prefix="hyvv-status-")
        for n in ("go.key", "deepseek.key"):
            (Path(tmp) / n).write_text("fake-key-not-real", encoding="utf-8")
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": tmp, "backoff_s": [0.2],
               "model": "deepseek-v4.1-flash", "base_url": "http://fake.invalid", "wire": "openai",
               "image": "cua-bee:status", "runtime": "runc", "memory_mb": 512, "projects": {"status-demo": {}}}
        cls.la = L.Launcher(cfg, L.FakeBackend(cfg))
        threading.Thread(target=cls.la.loop.run_forever, daemon=True).start()
        cls.token = "status-" + uuid.uuid4().hex * 2
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(cls.la, cls.token))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.aid = "att_" + uuid.uuid4().hex[:16]
        st, _ = cls.req("POST", "/v1/attempts", {"attempt_id": cls.aid, "brief": {"objective": "status probe", "project": "status-demo"}})
        assert st == 201, st
        t0 = time.time()
        while cls.la.attempts[cls.aid]["state"] not in L.TERMINAL and time.time() - t0 < 10:
            time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    @classmethod
    def req(cls, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", cls.srv.server_address[1], timeout=10)
        data = json.dumps(body if body is not None else {}).encode() if method in ("POST", "PUT", "PATCH", "DELETE") else None
        c.request(method, path, body=data, headers={"Authorization": "Bearer " + cls.token, "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw or b"{}")
        except ValueError:
            return r.status, {}

    def _route_exists(self, key: str) -> bool:
        method, path = key.split(" ", 1)
        path = path.replace("{attempt_id}", self.aid).replace("{lease_id}", "lease_status_probe")
        body = {"accepted": True} if path.endswith("/review") else ({"max_concurrent": 1} if path == "/v1/config" else None)
        if method == "POST" and path == "/v1/attempts":
            body = {"brief": {"objective": "x", "project": "status-not-authorized"}}
        st, b = self.req(method, path, body)
        missing = st == 501 or (st == 404 and b.get("error") == "not found")
        return not missing

    def test_launcher_labels_match_the_runtime(self):
        for key, label in S.STATUS["endpoints"].items():
            with self.subTest(endpoint=key, label=label):
                exists = self._route_exists(key)
                if label == "launcher-0.1.15":
                    self.assertTrue(exists, f"{key} is labelled launcher-0.1.15 but the launcher has no such route")
                else:
                    self.assertFalse(exists, f"{key} is served by the launcher; relabel it launcher-0.1.15")


if __name__ == "__main__":
    unittest.main()
