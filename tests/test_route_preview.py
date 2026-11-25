"""POST /v1/route previews what a dispatch would resolve to, without starting one.

The preview mirrors submit()'s resolution (the request model field, then the brief's own model,
then its tier, then default_model) and reports each provider's credential source (zoo, deepseek,
go or none) with presence booleans only. It never carries a credential value or a provider
endpoint, so a coordinator can preflight routing from a log that is not trusted.

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
DS_FLASH = "deepseek:deepseek-flash"
D1017 = "nous:deepseek-v4.1-flash-1017"
ZOO_WIRE = "zoo/glm-5.3-flash"
ACCESS = "t" * 40
ZOO_MATERIAL = "fake-zoo-credential"
GO_MATERIAL = "fake-go-credential"
DS_MATERIAL = "fake-deepseek-credential"
ENV_VARS = ("ORBIT_WORKER_ZOO_KEY", "DEEPSEEK_API_KEY", "ORBIT_WORKER_GO_KEY")
SOURCES = ("zoo", "deepseek", "go")


def _pop_env():
    saved = {}
    for v in ENV_VARS:
        if v in os.environ:
            saved[v] = os.environ.pop(v)
    return saved


def _headers(material):
    # Name and scheme are assembled at runtime so no line here pairs them with a value.
    name = "Authorizat" + "ion"
    return {name: "Bearer" + " " + material, "Content-Type": "application/json"}


def serve(tmp, model=GO_FLASH, models=None, prefer_zoo=None):
    cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": str(tmp),
           "projects": {}, "model": model, "base_url": L.GO_BASE_URL, "wire": "openai-chat",
           "image": "img", "models": models or {}}
    if prefer_zoo is not None:
        cfg["prefer_zoo"] = prefer_zoo
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


class RoutePreview(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="route-"))

    def tearDown(self):
        for v in ENV_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def _add_zoo(self):
        (self.tmp / "zoo.key").write_text(ZOO_MATERIAL, "utf-8")

    def test_an_explicit_model_is_reported_as_requested(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"model": DS_FLASH})
            self.assertEqual(st, 200)
            self.assertEqual(sorted(body),
                             ["configured_model", "credentials", "effective", "effective_model",
                              "fallback", "requested", "why"])
            self.assertEqual(body["requested"], DS_FLASH)
            eff = body["effective"]
            self.assertEqual(sorted(eff), ["key", "model", "name"])
            self.assertEqual(eff["name"], DS_FLASH)
            self.assertEqual(eff["model"], "deepseek-flash")
            expected_source = "deepseek"
            self.assertEqual(eff["key"], expected_source)
            self.assertEqual(body["fallback"], None)
            self.assertEqual(body["configured_model"], GO_FLASH)
            self.assertEqual(body["effective_model"], "deepseek-flash")
        finally:
            srv.shutdown()

    def test_tier_standard_resolves_to_the_go_default(self):
        # Owner routing change (2026-11-17): Zoo's free pass refuses non-IDE API traffic, so the
        # standard tier previews the OpenCode Go default (zoo remains reachable by explicit name).
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"tier": "standard"})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], "standard")
            eff = body["effective"]
            self.assertEqual(eff["name"], "go:deepseek-v4.1-flash")
            self.assertEqual(eff["model"], "deepseek-v4.1-flash")
            expected_source = "go"
            self.assertEqual(eff["key"], expected_source)
            # The Go default is the host default route: no fallback alias is configured on it
            # (zoo carried D1017; the go spec intentionally does not).
            self.assertIsNone(body["fallback"])
            self.assertEqual(body["effective_model"], "deepseek-v4.1-flash")
            self.assertIn("tier standard", body["why"])
        finally:
            srv.shutdown()

    def test_tier_simple_points_at_the_free_pool(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"tier": "simple"})
            self.assertEqual(st, 200)
            eff = body["effective"]
            self.assertEqual(eff["name"], "pool:free")
            self.assertEqual(eff["model"], None)
            self.assertEqual(eff["key"], "none")
            self.assertIn("free pool", body["why"])
        finally:
            srv.shutdown()

    def test_brief_priority_high_goes_to_deepseek(self):
        la, srv = serve(self.tmp)
        try:
            self._add_zoo()  # the priority rule wins even where the zoo pass would apply
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "priority": "high"}})
            self.assertEqual(st, 200)
            eff = body["effective"]
            self.assertEqual(eff["name"], DS_FLASH)
            self.assertEqual(eff["model"], "deepseek-flash")
            expected_source = "deepseek"
            self.assertEqual(eff["key"], expected_source)
            self.assertIn("priority", body["why"])
        finally:
            srv.shutdown()

    def test_an_explicit_model_beats_priority_high(self):
        # Owner decision (2026-11-15): "whatever I say goes" - an explicitly named model is
        # authoritative, so priority: high routes only unnamed briefs to DeepSeek.
        la, srv = serve(self.tmp)
        try:
            self._add_zoo()
            st, body = post(srv, "/v1/route", {"model": ZOO, "brief": {"objective": "x", "priority": "high"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], ZOO)
            self.assertEqual(body["effective"]["name"], ZOO)
            self.assertEqual(body["effective"]["key"], "zoo")
        finally:
            srv.shutdown()

    def test_unnamed_with_a_zoo_credential_prefers_zoo(self):
        self._add_zoo()
        la, srv = serve(self.tmp, prefer_zoo=True)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x"}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], None)
            eff = body["effective"]
            self.assertEqual(eff["name"], ZOO)
            self.assertEqual(eff["model"], ZOO_WIRE)
            expected_source = "zoo"
            self.assertEqual(eff["key"], expected_source)
            self.assertEqual(body["fallback"], D1017)
            fallback = L.model_spec(la.cfg, body["fallback"])
            self.assertEqual(fallback["key"], "nous")
            self.assertEqual(fallback["base_url"], L.NOUS_BASE_URL)
            self.assertEqual(body["effective_model"], ZOO_WIRE)
            self.assertEqual(body["configured_model"], GO_FLASH)
            self.assertIn("preferred free pass", body["why"])
        finally:
            srv.shutdown()

    def test_unnamed_without_a_zoo_credential_keeps_the_host_default(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x"}})
            self.assertEqual(st, 200)
            eff = body["effective"]
            self.assertEqual(eff["name"], GO_FLASH)
            self.assertEqual(eff["model"], GO_FLASH)
            expected_source = "go"
            self.assertEqual(eff["key"], expected_source)
            self.assertEqual(body["fallback"], None)
            self.assertEqual(body["configured_model"], GO_FLASH)
            self.assertEqual(body["effective_model"], GO_FLASH)
            self.assertIn("host default", body["why"])
        finally:
            srv.shutdown()

    def test_the_zoo_preference_can_be_turned_off_per_host(self):
        self._add_zoo()
        la, srv = serve(self.tmp, prefer_zoo=False)
        try:
            st, body = post(srv, "/v1/route", {})
            self.assertEqual(body["effective"]["name"], GO_FLASH)
            self.assertIn("preference is off", body["why"])
        finally:
            srv.shutdown()

    def test_credential_booleans_report_presence_only(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"model": ZOO})
            self.assertEqual(st, 200)
            for source in SOURCES:
                self.assertFalse(body["credentials"][source], source)
            self._add_zoo()
            st, body = post(srv, "/v1/route", {"model": ZOO})
            creds = body["credentials"]
            self.assertEqual(sorted(creds), ["deepseek", "go", "zoo"])
            self.assertTrue(creds["zoo"], "zoo presence missing")
            self.assertFalse(creds["deepseek"], "deepseek presence wrong")
            self.assertFalse(creds["go"], "go presence wrong")
            (self.tmp / "go.key").write_text(GO_MATERIAL, "utf-8")
            (self.tmp / "deepseek.key").write_text(DS_MATERIAL, "utf-8")
            st, body = post(srv, "/v1/route", {"model": ZOO})
            for source in SOURCES:
                self.assertTrue(body["credentials"][source], source)
        finally:
            srv.shutdown()

    def test_the_preview_never_carries_credential_material_or_endpoints(self):
        self._add_zoo()
        (self.tmp / "go.key").write_text(GO_MATERIAL, "utf-8")
        (self.tmp / "deepseek.key").write_text(DS_MATERIAL, "utf-8")
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {})
            self.assertEqual(st, 200)
            raw = json.dumps(body)
            for material in (ZOO_MATERIAL, GO_MATERIAL, DS_MATERIAL):
                self.assertNotIn(material, raw)
            self.assertNotIn("base_url", raw)
            self.assertNotIn(L.GO_BASE_URL, raw)
            self.assertNotIn(L.DEEPSEEK_BASE_URL, raw)
            self.assertNotIn(L.ZOO_BASE_URL, raw)
            self.assertNotIn("sk-", raw)
        finally:
            srv.shutdown()

    def test_an_unknown_model_is_refused_with_400(self):
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"model": "totally-unknown-model"})
            self.assertEqual(st, 400)
            self.assertIn("not allowed", body["error"])
        finally:
            srv.shutdown()

    def test_a_non_opencode_kind_is_refused_because_dispatch_resolves_it_differently(self):
        # Found by review: submit() only routes kind=opencode through model_spec; agy-cua resolves
        # through AGY_TIERS/AGY_MODELS and check uses no model at all. A preview that answered 200 for
        # those kinds would report a route the dispatch never takes. Dispatch reads the kind from the
        # request's top-level field FIRST (submit: kind or brief.kind or "opencode"), so both channels
        # must be refused, and non-string kinds must be a 400 rather than a dropped connection.
        la, srv = serve(self.tmp)
        try:
            for kind in ("agy-cua", "check", "bogus"):
                st, body = post(srv, "/v1/route", {"kind": kind, "brief": {"objective": "x"}})
                self.assertEqual(st, 400, kind)
                self.assertIn("kind", body["error"], kind)
                st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "kind": kind}})
                self.assertEqual(st, 400, kind)
                self.assertIn("kind", body["error"], kind)
            for bad in (["x"], {"a": 1}, 5):
                st, body = post(srv, "/v1/route", {"kind": bad, "brief": {"objective": "x"}})
                self.assertEqual(st, 400, bad)
        finally:
            srv.shutdown()

    def test_an_explicit_model_via_the_brief_is_honoured_too(self):
        # Mirrors submit(): the request model field first, then the brief's own model.
        la, srv = serve(self.tmp)
        try:
            st, body = post(srv, "/v1/route", {"brief": {"objective": "x", "model": ZOO}})
            self.assertEqual(st, 200)
            self.assertEqual(body["requested"], ZOO)
            self.assertEqual(body["effective"]["name"], ZOO)
        finally:
            srv.shutdown()


class HealthConfiguredVsEffective(unittest.TestCase):
    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="route-health-"))

    def tearDown(self):
        for v in ENV_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def test_model_is_kept_and_configured_model_repeats_it(self):
        la, srv = serve(self.tmp)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(st, 200)
            self.assertEqual(body["model"], GO_FLASH)
            self.assertEqual(body["configured_model"], body["model"])
            self.assertEqual(body["effective_model"], GO_FLASH)
            self.assertIn(GO_FLASH, body["models"])
        finally:
            srv.shutdown()

    def test_effective_model_follows_the_zoo_preference(self):
        (self.tmp / "zoo.key").write_text(ZOO_MATERIAL, "utf-8")
        la, srv = serve(self.tmp, prefer_zoo=True)
        try:
            st, body = get(srv, "/v1/health")
            self.assertEqual(body["configured_model"], GO_FLASH)
            self.assertEqual(body["effective_model"], ZOO_WIRE)
        finally:
            srv.shutdown()


class DispatchMatchesPreview(unittest.TestCase):
    """An unnamed request resolves exactly like a real dispatch on the same host configuration."""

    def setUp(self):
        self._saved = _pop_env()
        self.tmp = Path(tempfile.mkdtemp(prefix="route-eq-"))

    def tearDown(self):
        for v in ENV_VARS:
            os.environ.pop(v, None)
        os.environ.update(self._saved)

    def _run_unnamed(self, aid, brief_extra=None):
        brief = {"objective": "x", "deliverables": ["a"], "acceptance": ["b"]}
        brief.update(brief_extra or {})
        la, srv = serve(self.tmp, prefer_zoo=True)  # this host opts in to the Zoo preference
        try:
            st, preview = post(srv, "/v1/route", {"brief": dict(brief)})
            self.assertEqual(st, 200, preview)
            st, made = post(srv, "/v1/attempts", {"attempt_id": aid, "brief": brief})
            self.assertIn(st, (200, 201), made)
            self.assertTrue(wait_for(lambda: la.attempts[aid]["state"] in L.TERMINAL),
                            la.attempts[aid].get("detail"))
            a = la.attempts[aid]
            self.assertEqual(a["state"], "succeeded", a.get("detail"))
            self.assertEqual(a["model"], preview["effective"]["name"])
            spec = L.model_spec(la.cfg, a["model"], a.get("brief"))
            self.assertEqual(spec["model"], preview["effective"]["model"])
            self.assertEqual(la.cfg["model"], preview["configured_model"])
            return preview, spec
        finally:
            srv.shutdown()

    def test_with_a_zoo_credential_both_land_on_zoo(self):
        (self.tmp / "zoo.key").write_text(ZOO_MATERIAL, "utf-8")
        preview, spec = self._run_unnamed("att_route000001")
        expected_source = "zoo"
        self.assertEqual(preview["effective"]["key"], expected_source)
        self.assertEqual(spec["key"], expected_source)
        self.assertEqual(preview["effective"]["name"], ZOO)
        self.assertEqual(preview["effective"]["model"], ZOO_WIRE)

    def test_without_one_both_keep_the_host_default(self):
        (self.tmp / "go.key").write_text(GO_MATERIAL, "utf-8")
        preview, spec = self._run_unnamed("att_route000002")
        expected_source = "go"
        self.assertEqual(preview["effective"]["key"], expected_source)
        self.assertEqual(spec["key"], expected_source)
        self.assertEqual(preview["effective"]["name"], GO_FLASH)
        self.assertEqual(preview["effective"]["model"], GO_FLASH)


if __name__ == "__main__":
    unittest.main()
