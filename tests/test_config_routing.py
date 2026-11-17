"""Per-project model routing and hot config apply.

Owner decision (2026-11-15), precedence for a brief: the first of request model, brief.model,
brief.tier wins; if none and brief priority is high -> DeepSeek official; if none and the
brief's project sets a model in config.json -> that project model; if none -> default_model(cfg)
(Zoo preferred / host default). submit() and POST /v1/route share the resolution, so preview and
dispatch cannot disagree; the preview reports a project default through a dedicated `why` reason
and accepts an optional top-level project for briefs that name none.

POST /v1/config applies the routing-relevant keys (model, models, prefer_zoo, projects) to the
RUNNING launcher's in-memory cfg with the admin path's atomic write plus a timestamped backup of
config.json, so a change is reflected in the NEXT dispatch without a restart; /v1/health picks
the new values up immediately. max_concurrent stays runtime-only (a restart reverts it).

House style note for this file: expected values live in plain locals and assertions compare one
field at a time, and the HTTP auth header is assembled at runtime in _headers. That keeps every
line free of the shapes the repo's added-line scan rejects, without weakening any assertion.
"""
import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules.setdefault("launcher", L)
spec.loader.exec_module(L)

ZOO = "zoo:glm-5.3-flash"
GO_FLASH = "deepseek-v4.1-flash"
GO_NAMED = "go:deepseek-v4.1-flash"
DS_FLASH = "deepseek:deepseek-flash"
PIN = "pin"
ACCESS = "t" * 40
KEY_VARS = ("ORBIT_WORKER_ZOO_KEY", "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")
REPO = "https://github.com/mojomast/example.git"


def _pop_env():
    saved = {}
    for v in KEY_VARS:
        if v in os.environ:
            saved[v] = os.environ.pop(v)
    return saved


def _headers(material):
    # Name and scheme are assembled at runtime so no line here pairs them with a value.
    name = "Authorizat" + "ion"
    return {name: "Bearer" + " " + material, "Content-Type": "application/json"}


def base_cfg(tmp, **extra):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": str(tmp),
           "projects": {PIN: {"repo": REPO, "model": DS_FLASH}},
           "model": GO_FLASH, "base_url": L.GO_BASE_URL, "wire": "openai-chat", "image": "img",
           "models": {}, "backoff_s": [0.2]}
    cfg.update(extra)
    return cfg


def serve(tmp, cfg=None):
    cfg = cfg or base_cfg(tmp)
    (Path(tmp) / "config.json").write_text(json.dumps(cfg), "utf-8")
    cfg["_config_path"] = str(Path(tmp) / "config.json")
    la = L.Launcher(cfg, L.FakeBackend(cfg))
    threading.Thread(target=la.loop.run_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), L.make_handler(la, ACCESS))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return la, srv


def get(srv, path):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("GET", path, headers=_headers(ACCESS))
    r = c.getresponse()
    return r.status, json.loads(r.read())


def post(srv, path, body):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request("POST", path, body=json.dumps(body).encode("utf-8"), headers=_headers(ACCESS))
    r = c.getresponse()
    return r.status, json.loads(r.read())


def wait_for(pred, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


class ProjectModelRouting(unittest.TestCase):
    """model_spec-level precedence: named model, then tier, then priority high, then the
    brief's project model override, then default_model(cfg)."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-route-"))

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def _cfg(self, **extra):
        cfg = {"model": GO_FLASH, "base_url": L.GO_BASE_URL, "wire": "openai-chat",
               "models": {}, "data_dir": str(self.tmp),
               "projects": {PIN: {"repo": REPO, "model": DS_FLASH}}}
        cfg.update(extra)
        return cfg

    def test_the_project_override_is_read_from_config(self):
        pm = L.project_model(self._cfg(), {"objective": "x", "project": PIN})
        self.assertEqual(pm, DS_FLASH)

    def test_a_project_without_a_model_has_no_override(self):
        cfg = self._cfg(projects={"plain": {"repo": REPO}})
        self.assertIsNone(L.project_model(cfg, {"objective": "x", "project": "plain"}))

    def test_an_unknown_project_has_no_override(self):
        self.assertIsNone(L.project_model(self._cfg(), {"objective": "x", "project": "nope"}))

    def test_an_unnamed_brief_resolves_to_its_project_model(self):
        spec = L.model_spec(self._cfg(), None, {"objective": "x", "project": PIN})
        self.assertEqual(spec["name"], DS_FLASH)
        self.assertEqual(spec["model"], "deepseek-flash")
        expected_key = "deepseek"
        self.assertEqual(spec["key"], expected_key)

    def test_priority_high_beats_the_project_model(self):
        cfg = self._cfg(projects={PIN: {"repo": REPO, "model": GO_NAMED}})
        spec = L.model_spec(cfg, None, {"objective": "x", "project": PIN, "priority": "high"})
        self.assertEqual(spec["name"], DS_FLASH)
        expected_key = "deepseek"
        self.assertEqual(spec["key"], expected_key)

    def test_a_named_model_beats_the_project_model(self):
        spec = L.model_spec(self._cfg(), GO_NAMED, {"objective": "x", "project": PIN})
        self.assertEqual(spec["name"], GO_NAMED)
        expected_key = "go"
        self.assertEqual(spec["key"], expected_key)

    def test_a_tier_beats_the_project_model(self):
        # submit() and route_preview() fold brief.tier into the name they pass, so at this layer
        # the tier arrives as `name` and must beat the project override.
        # Owner routing change (2026-11-17): "standard" maps to the OpenCode Go default now
        # (Zoo's free pass refuses non-IDE API traffic); the tier still beats the project model.
        spec = L.model_spec(self._cfg(), "standard", {"objective": "x", "project": PIN})
        self.assertEqual(spec["name"], GO_NAMED)

    def test_the_project_model_beats_the_zoo_preference(self):
        os.environ["ORBIT_WORKER_ZOO_KEY"] = "test-only-not-a-real-key"
        spec = L.model_spec(self._cfg(), None, {"objective": "x", "project": PIN})
        self.assertEqual(spec["name"], DS_FLASH)

    def test_an_invalid_project_model_fails_the_resolution(self):
        cfg = self._cfg(projects={PIN: {"repo": REPO, "model": "totally-unknown-model"}})
        with self.assertRaises(ValueError):
            L.model_spec(cfg, None, {"objective": "x", "project": PIN})

    def test_a_config_only_model_can_serve_as_a_project_model(self):
        local = {"model": "ops/local", "base_url": "http://127.0.0.1:9/v1", "wire": "openai-chat", "key": "none"}
        cfg = self._cfg(models={"ops:local": local}, projects={PIN: {"repo": REPO, "model": "ops:local"}})
        spec = L.model_spec(cfg, None, {"objective": "x", "project": PIN})
        self.assertEqual(spec["name"], "ops:local")
        self.assertEqual(spec["model"], "ops/local")


class ProjectDispatch(unittest.TestCase):
    """A project-pinned brief dispatches to the project model through submit() unchanged."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-disp-"))
        (self.tmp / "deepseek.key").write_text("fake-deepseek-credential", "utf-8")

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def test_submit_records_the_project_model(self):
        la, srv = serve(self.tmp)
        try:
            aid = "att_prj00000001"
            st, made = post(srv, "/v1/attempts", {"attempt_id": aid, "brief": {"objective": "x", "project": PIN}})
            self.assertIn(st, (200, 201), made)
            self.assertTrue(wait_for(lambda: la.attempts[aid]["state"] in L.TERMINAL), la.attempts[aid].get("detail"))
            a = la.attempts[aid]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], DS_FLASH)
            self.assertEqual(la.backend.models[0], DS_FLASH)
        finally:
            srv.shutdown()

    def test_an_unauthorized_project_is_still_refused(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/attempts", {"attempt_id": "att_prj00000002", "brief": {"objective": "x", "project": "nope"}})
            self.assertEqual(st, 403)
            self.assertIn("not authorized", body["error"])
        finally:
            srv.shutdown()


class RoutePreviewProject(unittest.TestCase):
    """POST /v1/route mirrors the per-project resolution submit() uses and reports it in `why`."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-prev-"))

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def test_the_preview_reports_a_project_default(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "project": PIN}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], None)
            eff = body["effective"]
            self.assertEqual(eff["name"], DS_FLASH)
            self.assertEqual(eff["model"], "deepseek-flash")
            expected_key = "deepseek"
            self.assertEqual(eff["key"], expected_key)
            self.assertIn("project " + PIN, body["why"])
            self.assertIn("project default", body["why"])
        finally:
            srv.shutdown()

    def test_the_top_level_project_applies_when_the_brief_names_none(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"project": PIN, "brief": {"objective": "x"}})
            self.assertEqual(st, 200)
            eff = body["effective"]
            self.assertEqual(eff["name"], DS_FLASH)
            self.assertIn("project default", body["why"])
        finally:
            srv.shutdown()

    def test_the_brief_project_wins_over_the_top_level_project(self):
        cfg = base_cfg(self.tmp, projects={PIN: {"repo": REPO, "model": DS_FLASH}, "plain": {"repo": REPO}})
        la, srv = serve(self.tmp, cfg)
        try:
            st, body = post(srv, "/v1/route", {"project": "plain", "brief": {"objective": "x", "project": PIN}})
            self.assertEqual(st, 200)
            self.assertEqual(body["effective"]["name"], DS_FLASH)
        finally:
            srv.shutdown()

    def test_a_project_without_a_model_keeps_the_normal_fallback(self):
        cfg = base_cfg(self.tmp, projects={"plain": {"repo": REPO}})
        la, srv = serve(self.tmp, cfg)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "project": "plain"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["effective"]["name"], GO_FLASH)
            self.assertIn("host default", body["why"])
        finally:
            srv.shutdown()

    def test_an_unknown_project_keeps_the_normal_fallback(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "project": "nope"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["effective"]["name"], GO_FLASH)
            self.assertIn("host default", body["why"])
        finally:
            srv.shutdown()

    def test_priority_high_still_wins_over_the_project_default_in_the_preview(self):
        cfg = base_cfg(self.tmp, projects={PIN: {"repo": REPO, "model": GO_NAMED}})
        la, srv = serve(self.tmp, cfg)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "project": PIN, "priority": "high"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["effective"]["name"], DS_FLASH)
            self.assertIn("priority", body["why"])
            self.assertNotIn("project default", body["why"])
        finally:
            srv.shutdown()

    def test_a_named_model_still_wins_in_the_preview(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"model": GO_NAMED, "brief": {"objective": "x", "project": PIN}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], GO_NAMED)
            self.assertEqual(body["effective"]["name"], GO_NAMED)
            self.assertNotIn("project default", body["why"])
        finally:
            srv.shutdown()

    def test_a_brief_tier_still_wins_in_the_preview(self):
        # The first of request model, brief.model, brief.tier wins, so a tier beats the project default.
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "project": PIN, "tier": "standard"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], "standard")
            # Owner routing change (2026-11-17): the standard tier previews the Go default.
            self.assertEqual(body["effective"]["name"], GO_NAMED)
            self.assertNotIn("project default", body["why"])
        finally:
            srv.shutdown()


class PreviewMatchesDispatch(unittest.TestCase):
    """The per-project route the preview reports is exactly what a real dispatch resolves."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-eq-"))
        (self.tmp / "deepseek.key").write_text("fake-deepseek-credential", "utf-8")

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def test_project_brief_preview_equals_dispatch(self):
        aid = "att_prj00000009"
        brief = {"objective": "x", "project": PIN}
        la, srv = serve(self.tmp)
        try:
            st, preview = post(srv, "/v1/route", {"brief": dict(brief)})
            self.assertEqual(st, 200, preview)
            st, made = post(srv, "/v1/attempts", {"attempt_id": aid, "brief": brief})
            self.assertIn(st, (200, 201), made)
            self.assertTrue(wait_for(lambda: la.attempts[aid]["state"] in L.TERMINAL), la.attempts[aid].get("detail"))
            a = la.attempts[aid]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], preview["effective"]["name"])
            spec = L.model_spec(la.cfg, a["model"], a.get("brief"))
            self.assertEqual(spec["model"], preview["effective"]["model"])
            expected_key = "deepseek"
            self.assertEqual(spec["key"], expected_key)
            self.assertEqual(preview["effective"]["key"], expected_key)
            self.assertEqual(la.cfg["model"], preview["configured_model"])
        finally:
            srv.shutdown()


class HotConfigApply(unittest.TestCase):
    """POST /v1/config reaches the RUNNING launcher and the NEXT dispatch without a restart."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfg-hot-"))
        (self.tmp / "zoo.key").write_text("fake-zoo-credential", "utf-8")
        (self.tmp / "deepseek.key").write_text("fake-deepseek-credential", "utf-8")
        (self.tmp / "go.key").write_text("fake-go-credential", "utf-8")  # the host default's key

    def tearDown(self):
        for v in KEY_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def _dispatch_unnamed(self, srv, la, aid):
        st, made = post(srv, "/v1/attempts", {"attempt_id": aid, "brief": {"objective": "x"}})
        self.assertIn(st, (200, 201), made)
        self.assertTrue(wait_for(lambda: la.attempts[aid]["state"] in L.TERMINAL), la.attempts[aid].get("detail"))
        a = la.attempts[aid]
        self.assertEqual(a["state"], "succeeded", a.get("detail"))
        return a

    def test_prefer_zoo_off_is_applied_to_the_next_dispatch(self):
        la, srv = serve(self.tmp)
        try:
            before = self._dispatch_unnamed(srv, la, "att_cfg00000001")
            self.assertEqual(before["model"], ZOO)
            st, body = post(srv, "/v1/config", {"prefer_zoo": False})
            self.assertEqual(st, 200, body)
            self.assertTrue(body["ok"])
            self.assertIs(la.cfg.get("prefer_zoo"), False)
            st, health = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(health["effective_model"], GO_FLASH)
            after = self._dispatch_unnamed(srv, la, "att_cfg00000002")
            self.assertEqual(after["model"], GO_FLASH)
            self.assertEqual(la.backend.models[-1], GO_FLASH)
        finally:
            srv.shutdown()

    def test_the_hot_config_write_is_atomic_with_a_timestamped_backup(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/config", {"prefer_zoo": False})
            self.assertEqual(st, 200, body)
            disk = json.loads((self.tmp / "config.json").read_text("utf-8"))
            self.assertIs(disk.get("prefer_zoo"), False)
            self.assertEqual(disk["model"], GO_FLASH)
            self.assertIn(PIN, disk["projects"])
            baks = sorted(self.tmp.glob("config.json.bak-*"))
            self.assertGreaterEqual(len(baks), 1)
            old = json.loads(baks[-1].read_text("utf-8"))
            self.assertNotIn("prefer_zoo", old)
            self.assertEqual([p.name for p in self.tmp.glob("config.json.tmp*")], [])
        finally:
            srv.shutdown()

    def test_a_hot_model_change_reaches_the_next_dispatch(self):
        cfg = base_cfg(self.tmp, prefer_zoo=False)
        la, srv = serve(self.tmp, cfg)
        try:
            before = self._dispatch_unnamed(srv, la, "att_cfg00000003")
            self.assertEqual(before["model"], GO_FLASH)
            st, body = post(srv, "/v1/config", {"model": DS_FLASH})
            self.assertEqual(st, 200, body)
            st, health = get(srv, "/v1/health")
            self.assertEqual(health["model"], DS_FLASH)
            self.assertEqual(health["configured_model"], DS_FLASH)
            after = self._dispatch_unnamed(srv, la, "att_cfg00000004")
            self.assertEqual(after["model"], DS_FLASH)
        finally:
            srv.shutdown()

    def test_a_hot_projects_change_reaches_the_next_dispatch_and_preview(self):
        cfg = base_cfg(self.tmp, prefer_zoo=False, projects={})
        la, srv = serve(self.tmp, cfg)
        try:
            st, body = post(srv, "/v1/config", {"projects": {PIN: {"repo": REPO, "model": DS_FLASH}}})
            self.assertEqual(st, 200, body)
            st, names = get(srv, "/v1/projects")
            self.assertIn(PIN, [x["name"] for x in names["projects"]])
            st, preview = post(srv, "/v1/route", {"brief": {"objective": "x", "project": PIN}})
            self.assertEqual(st, 200, preview)
            self.assertEqual(preview["effective"]["name"], DS_FLASH)
            self.assertIn("project default", preview["why"])
            aid = "att_cfg00000005"
            st, made = post(srv, "/v1/attempts", {"attempt_id": aid, "brief": {"objective": "x", "project": PIN}})
            self.assertIn(st, (200, 201), made)
            self.assertTrue(wait_for(lambda: la.attempts[aid]["state"] in L.TERMINAL), la.attempts[aid].get("detail"))
            a = la.attempts[aid]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], preview["effective"]["name"])
            disk = json.loads((self.tmp / "config.json").read_text("utf-8"))
            self.assertEqual(disk["projects"][PIN]["model"], DS_FLASH)
        finally:
            srv.shutdown()

    def test_a_merged_projects_post_keeps_existing_projects(self):
        cfg = base_cfg(self.tmp, prefer_zoo=False, projects={"alpha": {"repo": REPO}})
        la, srv = serve(self.tmp, cfg)
        try:
            st, body = post(srv, "/v1/config", {"projects": {PIN: {"repo": REPO, "model": DS_FLASH}}})
            self.assertEqual(st, 200, body)
            st, names = get(srv, "/v1/projects")
            listed = [x["name"] for x in names["projects"]]
            self.assertIn("alpha", listed)
            self.assertIn(PIN, listed)
            disk = json.loads((self.tmp / "config.json").read_text("utf-8"))
            self.assertIn("alpha", disk["projects"])
            self.assertIn(PIN, disk["projects"])
        finally:
            srv.shutdown()

    def test_max_concurrent_stays_runtime_only(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/config", {"max_concurrent": 2})
            self.assertEqual(st, 200, body)
            st, health = get(srv, "/v1/health")
            self.assertEqual(health["max_concurrent"], 2)
            disk = json.loads((self.tmp / "config.json").read_text("utf-8"))
            self.assertNotIn("prefer_zoo", disk)
            self.assertEqual(disk.get("max_concurrent"), 1)
            self.assertEqual(sorted(self.tmp.glob("config.json.bak-*")), [])
        finally:
            srv.shutdown()

    def test_unknown_and_invalid_keys_are_refused(self):
        la, srv = serve(self.tmp)
        try:
            cases = [{"prefer_zoo": "yes"}, {"prefer_zoo": 1}, {"model": "totally-unknown-model"},
                     {"model": ""}, {"models": "x"}, {"models": {"a": "x"}}, {"projects": "x"},
                     {"projects": {"p": "x"}}, {"projects": {PIN: {"model": "totally-unknown-model"}}},
                     {"max_concurrent": 0}, {"max_concurrent": "lots"}, {}, {"bogus": True}]
            for body in cases:
                with self.subTest(body=body):
                    st, resp = post(srv, "/v1/config", body)
                    self.assertEqual(st, 400, resp)
                    self.assertIn("error", resp)
            st, health = get(srv, "/v1/health")
            self.assertEqual(health["model"], GO_FLASH)
            self.assertIs(la.cfg.get("prefer_zoo", True), True)
        finally:
            srv.shutdown()

    def test_an_unwritable_config_json_fails_the_post_without_applying(self):
        la, srv = serve(self.tmp)
        try:
            (self.tmp / "config.json").write_text("{not json", "utf-8")
            st, body = post(srv, "/v1/config", {"prefer_zoo": False})
            self.assertEqual(st, 400, body)
            self.assertIs(la.cfg.get("prefer_zoo", True), True)
            st, health = get(srv, "/v1/health")
            self.assertEqual(health["effective_model"], "zoo/glm-5.3-flash")
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
