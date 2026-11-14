"""Durable canonical rizzstack.bee-landed/1 terminal events: one per attempt, replayable outbox.

When WORKFLOW_SRC points at a pinned workflow checkout, every event is validated by
that checkout's canonical tools/bee_landed.py validator. Without it, the same
contract is enforced locally and any deviation fails closed (nothing is skipped).
"""
import hashlib, importlib.util, json, os, re, socket, sys, tempfile, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher_landing", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher_landing"] = L
spec.loader.exec_module(L)

AID = "att_landing0001"
DATA_KEYS = {"schema", "feature_id", "job_id", "task_id", "attempt_id", "coordinator",
             "backend", "state", "source_rev", "lease", "result"}
OPTIONAL_DATA_KEYS = {"terminal_transition_id", "correlation_id", "causation_id"}
ENVELOPE_KEYS = {"specversion", "id", "source", "type", "time", "subject",
                 "datacontenttype", "dataschema", "data"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_ID_RE = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
                         r"|urn:[A-Za-z0-9][A-Za-z0-9-]{0,31}:\S+)$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _load_canonical():
    src = os.environ.get("WORKFLOW_SRC")
    if not src:
        return None
    root = Path(src).expanduser()
    tool = root / "tools" / "bee_landed.py"
    schema = root / "schemas" / "bee-landed.schema.json"
    if not tool.is_file() or not schema.is_file():
        raise RuntimeError(f"WORKFLOW_SRC is set but canonical bee-landed files are missing under {root}")
    module_spec = importlib.util.spec_from_file_location("canonical_bee_landed", tool)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["canonical_bee_landed"] = module
    module_spec.loader.exec_module(module)
    module.SCHEMA_PATH = schema
    return module


CANONICAL = _load_canonical()
CANONICAL_SCHEMA = None
if CANONICAL is not None:
    CANONICAL_SCHEMA = json.loads(CANONICAL.SCHEMA_PATH.read_text(encoding="utf-8"))

try:
    import jsonschema
except ImportError:
    jsonschema = None


def _fail(message):
    raise AssertionError(message)


def assert_canonical(event):
    """Validate with the pinned canonical validator, or enforce the same contract locally."""
    if CANONICAL is not None:
        try:
            CANONICAL.validate(event)
        except Exception as exc:  # BeeLandedError carries the exact refusal code
            _fail(f"canonical validator refused event: {exc}")
        validator_cls = getattr(jsonschema, "Draft202012Validator", None) if jsonschema else None
        if CANONICAL_SCHEMA is not None and validator_cls is not None:
            try:
                validator_cls(CANONICAL_SCHEMA).validate(event)
            except jsonschema.ValidationError as exc:
                _fail(f"pinned schema refused event: {exc.message}")
        return

    if not isinstance(event, dict):
        _fail("event is not an object")
    for field in ("specversion", "id", "source", "type", "time", "subject", "data"):
        if field not in event:
            _fail(f"missing envelope field {field}")
    if event["specversion"] != "1.0" or event["type"] != "rizzstack.bee.landed.v1":
        _fail("wrong specversion or type")
    if not isinstance(event["id"], str) or not EVENT_ID_RE.match(event["id"]):
        _fail("id is neither a UUID nor a URN")
    if not isinstance(event["source"], str) or not event["source"] or re.search(r"\s", event["source"]):
        _fail("source is not a URI-reference")
    if not isinstance(event["time"], str) or not TIME_RE.match(event["time"]):
        _fail("time is not RFC 3339")
    if not isinstance(event["subject"], str) or not event["subject"]:
        _fail("subject is required")
    if event.get("datacontenttype", "application/json") != "application/json":
        _fail("datacontenttype must be application/json")
    data = event["data"]
    if not isinstance(data, dict):
        _fail("data is not an object")
    if set(data) - DATA_KEYS - OPTIONAL_DATA_KEYS:
        _fail(f"data has unknown fields: {sorted(set(data) - DATA_KEYS - OPTIONAL_DATA_KEYS)}")
    for field in DATA_KEYS:
        if field not in data:
            _fail(f"data is missing {field}")
    if data["schema"] != "rizzstack.bee-landed/1":
        _fail("data.schema is not rizzstack.bee-landed/1")
    for field in ("feature_id", "job_id", "task_id", "attempt_id"):
        if not isinstance(data[field], str) or not ID_RE.match(data[field]):
            _fail(f"data.{field} is not a stable id")
    coordinator = data["coordinator"]
    if not isinstance(coordinator, dict) or set(coordinator) - {"identity", "session"}:
        _fail("coordinator has unknown fields")
    if not isinstance(coordinator.get("identity"), str) or not coordinator["identity"]:
        _fail("coordinator.identity is required")
    backend = data["backend"]
    if not isinstance(backend, dict) or set(backend) - {"harness", "model"}:
        _fail("backend has unknown fields")
    if not isinstance(backend.get("harness"), str) or not backend["harness"]:
        _fail("backend.harness is required")
    if data["state"] not in ("succeeded", "failed", "cancelled"):
        _fail("state is not terminal")
    if not isinstance(data["source_rev"], str) or not REVISION_RE.match(data["source_rev"]):
        _fail("source_rev is not lowercase 40/64 hex")
    lease = data["lease"]
    if not isinstance(lease, dict) or set(lease) != {"sha256", "assigned_files"}:
        _fail("lease must carry exactly sha256 and assigned_files")
    if not isinstance(lease["sha256"], str) or not SHA256_RE.match(lease["sha256"]):
        _fail("lease.sha256 is not lowercase 64 hex")
    files = lease["assigned_files"]
    if not isinstance(files, list) or len(set(files)) != len(files):
        _fail("lease.assigned_files is not a unique array")
    for path in files:
        parts = path.split("/") if isinstance(path, str) else []
        if not parts or any(part in ("", ".", "..") for part in parts) or path.startswith("/"):
            _fail("lease.assigned_files contains an invalid path")
        if not re.match(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$", path):
            _fail("lease.assigned_files contains an invalid path")
    result = data["result"]
    if not isinstance(result, dict) or set(result) - {"available", "artifact_sha256", "reference"}:
        _fail("result has unknown fields")
    if not isinstance(result.get("available"), bool):
        _fail("result.available must be boolean")
    if result["available"] and not (result.get("artifact_sha256") or result.get("reference")):
        _fail("result.available is true without an artifact or reference")
    if "artifact_sha256" in result and (not isinstance(result["artifact_sha256"], str)
                                        or not SHA256_RE.match(result["artifact_sha256"])):
        _fail("result.artifact_sha256 is not lowercase 64 hex")
    for field in OPTIONAL_DATA_KEYS:
        if field in data and (not isinstance(data[field], str) or not data[field]):
            _fail(f"{field} must be a non-empty string")


def attempt(aid=AID, state="running", brief=None, model="deepseek-v4.1-flash"):
    b = {"objective": "SECRET-OBJECTIVE", "feature_id": "feat_1", "job_id": "job_1", "task_id": "task_1",
         "source_rev": "a" * 40, "coordinator": {"id": "orbit", "session": "sess_1"},
         "lease_sha256": "b" * 64}
    if brief:
        b.update(brief)
    return {"attempt_id": aid, "kind": "opencode", "brief": b, "state": state, "detail": "",
            "created_at": 100.0, "updated_at": 100.0, "lseq": 0, "model": model}


def write_result(la, aid=AID):
    path = la.dir / "attempts" / f"{aid}.result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "orbit.local-worker.result/v1", "attempt_id": aid}), "utf-8")
    return path


def outbox(la):
    return la.dir / "events" / "outbox"


def events(la):
    out = [json.loads(p.read_text("utf-8")) for p in sorted(outbox(la).glob("*.json"))]
    for event in out:
        assert_canonical(event)
    return out


def derived_lease_sha(assigned_files, source_rev):
    material = {"assigned_files": sorted(assigned_files), "source_rev": source_rev}
    return L._sha256_bytes(L._canonical_bytes(material))


class LandingEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="landed-")
        self.launchers = []

    def tearDown(self):
        for la in self.launchers:
            try:
                la.loop.close()
            except Exception:
                pass

    def mk(self, data_dir=None, **extra):
        cfg = {"bind": "127.0.0.1", "port": 0, "max_concurrent": 1, "data_dir": data_dir or self.tmp,
               "backoff_s": [0.1], "projects": {}, "model": "deepseek-v4.1-flash",
               "base_url": "http://x", "wire": "openai", "launcher_id": "worker-1", "image": "cua-bee:test"}
        cfg.update(extra)
        la = L.Launcher(cfg, L.FakeBackend(cfg))
        self.launchers.append(la)
        return la

    def add(self, la, aid=AID, state="running", brief=None):
        a = attempt(aid, state, brief)
        la.attempts[aid] = a
        la._save(a)
        return a

    def assert_exact_shape(self, ev, optional=()):
        self.assertEqual(set(ev), ENVELOPE_KEYS)
        self.assertEqual(set(ev["data"]), DATA_KEYS | set(optional))
        self.assertNotIn("event_id", ev)
        self.assertNotIn("event_id", ev["data"])
        for removed in ("harness", "model", "lease_sha256", "evidence_sha256"):
            self.assertNotIn(removed, ev)
            self.assertNotIn(removed, ev["data"])
        self.assertNotIn("id", ev["data"]["coordinator"])
        self.assertNotIn("schema", ev["data"]["result"])

    def test_succeeded_writes_one_canonical_cloudevent(self):
        la = self.mk()
        a = self.add(la)
        result = write_result(la)
        la._set(a, "succeeded", "done")

        evs = events(la)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assert_exact_shape(ev)
        self.assertEqual(ev["specversion"], "1.0")
        self.assertEqual(ev["type"], "rizzstack.bee.landed.v1")
        self.assertEqual(ev["subject"], AID)
        self.assertEqual(ev["source"], "hyvv://worker-1")
        self.assertTrue(ev["time"].endswith("Z"))
        self.assertEqual(ev["id"], next(outbox(la).glob("*.json")).stem)
        expected_id = "urn:rizzstack:bee-landed:" + hashlib.sha256(
            f"{AID}:succeeded:succeeded".encode("utf-8")).hexdigest()[:32]
        self.assertEqual(ev["id"], expected_id)

        d = ev["data"]
        self.assertEqual(d["schema"], "rizzstack.bee-landed/1")
        self.assertEqual((d["feature_id"], d["job_id"], d["task_id"], d["attempt_id"]),
                         ("feat_1", "job_1", "task_1", AID))
        self.assertEqual(d["coordinator"], {"identity": "orbit", "session": "sess_1"})
        self.assertEqual(d["backend"], {"harness": "opencode", "model": "deepseek-v4.1-flash"})
        self.assertEqual(d["state"], "succeeded")
        self.assertEqual(d["source_rev"], "a" * 40)
        self.assertEqual(d["lease"], {"sha256": "b" * 64, "assigned_files": []})
        self.assertEqual(d["result"], {
            "available": True,
            "artifact_sha256": hashlib.sha256(result.read_bytes()).hexdigest(),
            "reference": f"hyvv://attempts/{AID}/result",
        })

    def test_event_id_hashes_attempt_state_and_transition(self):
        transition = "transition-1"
        expected = "urn:rizzstack:bee-landed:" + hashlib.sha256(
            f"{AID}:succeeded:{transition}".encode("utf-8")).hexdigest()[:32]
        self.assertEqual(L.landed_event_id(AID, "succeeded", transition), expected)
        self.assertTrue(L.landed_event_id(AID, "succeeded").startswith("urn:rizzstack:bee-landed:"))

    def test_optional_correlation_and_causation_ids_are_kept(self):
        la = self.mk()
        a = self.add(la, brief={"correlation_id": "corr-1", "causation_id": "cause-1"})
        la._set(a, "failed", "controller error")
        ev = events(la)[0]
        self.assert_exact_shape(ev, optional=("correlation_id", "causation_id"))
        self.assertEqual(ev["data"]["correlation_id"], "corr-1")
        self.assertEqual(ev["data"]["causation_id"], "cause-1")

    def test_lease_digest_is_derived_from_sorted_files_and_source_rev(self):
        assigned = ["src/z.py", "src/a.py", "src/m.py"]
        la = self.mk()
        a = self.add(la, brief={"assigned_files": assigned, "lease_sha256": None})
        la._set(a, "failed", "controller error")
        d = events(la)[0]["data"]
        self.assertEqual(d["lease"]["assigned_files"], assigned)
        self.assertEqual(d["lease"]["sha256"], derived_lease_sha(assigned, "a" * 40))

    def test_finish_lifecycle_writes_event_after_result(self):
        la = self.mk()
        (Path(self.tmp) / "go.key").write_text("fake-provider-key", "utf-8")
        thread = threading.Thread(target=la.loop.run_forever, daemon=True)
        thread.start()
        try:
            aid = "att_lifecycle01"
            la.submit(aid, {"objective": "x", "source_rev": "a" * 40})
            deadline = time.time() + 5
            while time.time() < deadline and la.attempts[aid]["state"] not in L.TERMINAL:
                time.sleep(0.01)
            self.assertEqual(la.attempts[aid]["state"], "succeeded", la.attempts[aid].get("detail"))
            evs = events(la)
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0]["data"]["state"], "succeeded")
            self.assertTrue(evs[0]["data"]["result"]["available"])
        finally:
            la.loop.call_soon_threadsafe(la.loop.stop)
            thread.join(2)

    def test_duplicate_terminal_observation_does_not_create_a_second_event(self):
        la = self.mk()
        a = self.add(la)
        write_result(la)
        la._set(a, "succeeded", "done")
        path = next(outbox(la).glob("*.json"))
        before = path.read_bytes()

        la._set(a, "succeeded", "observed again")
        la.emit_landed_event(a)

        self.assertEqual(len(list(outbox(la).glob("*.json"))), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(events(la)), 1)

    def test_failed_event_has_no_result_artifact(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "failed", "controller error")
        ev = events(la)[0]
        self.assertEqual(ev["data"]["state"], "failed")
        self.assertEqual(ev["data"]["result"], {"available": False})

    def test_cancelled_event_is_emitted(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "cancelled", "interrupt confirmed")
        ev = events(la)[0]
        self.assertEqual(ev["data"]["state"], "cancelled")
        self.assertEqual(ev["data"]["result"], {"available": False})

    def test_crash_between_state_and_event_is_recovered_once(self):
        tmp = tempfile.mkdtemp(prefix="landed-crash-")
        attempts = Path(tmp) / "attempts"
        attempts.mkdir(parents=True)
        journal = attempt("att_crash00001", state="succeeded")
        (attempts / "att_crash00001.json").write_text(json.dumps(journal), "utf-8")
        (attempts / "att_crash00001.result.json").write_text(json.dumps({"schema": "orbit.local-worker.result/v1"}), "utf-8")

        la = self.mk(data_dir=tmp)
        evs = events(la)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["data"]["attempt_id"], "att_crash00001")
        self.assertEqual(evs[0]["data"]["state"], "succeeded")

        la2 = self.mk(data_dir=tmp)
        self.assertEqual(len(events(la2)), 1)
        self.assertEqual(la2.landing_conflicts, [])

    def test_conflicting_terminal_state_is_refused(self):
        la = self.mk()
        a = self.add(la)
        write_result(la)
        la._set(a, "succeeded", "done")
        path = next(outbox(la).glob("*.json"))
        before = path.read_bytes()

        a["state"] = "failed"
        with self.assertRaises(L.LandedEventConflict):
            la.emit_landed_event(a)

        self.assertEqual(len(list(outbox(la).glob("*.json"))), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_conflicting_terminal_state_through_set_is_refused(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "failed", "first")
        with self.assertRaises(L.LandedEventConflict):
            la._set(a, "cancelled", "second")
        self.assertEqual(len(events(la)), 1)
        self.assertEqual(events(la)[0]["data"]["state"], "failed")

    def test_conflicting_same_state_evidence_is_refused(self):
        la = self.mk()
        a = self.add(la)
        write_result(la)
        la._set(a, "succeeded", "done")
        path = next(outbox(la).glob("*.json"))
        before = path.read_bytes()

        a["brief"]["source_rev"] = "c" * 40
        with self.assertRaises(L.LandedEventConflict):
            la.emit_landed_event(a)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(events(la)), 1)

    def test_notifier_failure_leaves_durable_event(self):
        missing = Path(self.tmp) / "not-listening.sock"
        la = self.mk(landed_notifier={"type": "unix", "path": str(missing)})
        a = self.add(la)
        write_result(la)
        la._set(a, "succeeded", "done")

        self.assertEqual(len(events(la)), 1)
        self.assertEqual(events(la)[0]["data"]["attempt_id"], AID)
        self.assertFalse(missing.exists())

    def test_file_notifier_receives_one_signal_and_duplicate_is_not_re_signalled(self):
        notify = Path(self.tmp) / "notifications.jsonl"
        la = self.mk(landed_notifier={"type": "file", "path": str(notify)})
        a = self.add(la)
        write_result(la)
        la._set(a, "succeeded", "done")
        la.emit_landed_event(a)
        rows = [json.loads(line) for line in notify.read_text("utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        assert_canonical(rows[0])
        self.assertEqual(rows[0]["data"]["attempt_id"], AID)
        self.assertEqual(len(events(la)), 1)

    def test_unix_socket_notifier_receives_event(self):
        sock_path = Path(self.tmp) / "landed.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        server.listen(1)
        received = []

        def serve():
            conn, _ = server.accept()
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                received.append(data)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            la = self.mk(landed_notifier={"type": "unix", "path": str(sock_path)})
            a = self.add(la)
            la._set(a, "cancelled", "cancelled")
            thread.join(3)
        finally:
            server.close()
        self.assertEqual(len(received), 1)
        event = json.loads(received[0].decode("utf-8"))
        assert_canonical(event)
        self.assertEqual(event["data"]["attempt_id"], AID)
        self.assertEqual(event["data"]["state"], "cancelled")

    def test_no_notifier_still_leaves_event_for_replay(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "failed", "no notifier configured")
        self.assertEqual(len(events(la)), 1)
        ev = events(la)[0]
        self.assertFalse(L.notify_landed_event(ev, la.cfg))
        self.assertEqual(len(events(la)), 1)

    def test_event_excludes_tokens_private_paths_patches_and_logs(self):
        la = self.mk()
        secret = "sk-this-must-not-leak-123456789"
        private = "/home/alice/private/repo"
        a = self.add(la, brief={
            "objective": "SECRET-OBJECTIVE", "bbs_test_token": secret,
            "token": "Bearer abcdefghijklmnop", "patch": "diff --git a/secret b/secret",
            "logs": "private log output", "workdir": private,
            "lease_sha256": None, "lease": {"path": private, "id": "lease_1"},
        })
        write_result(la)
        la._set(a, "succeeded", "done")
        blob = json.dumps(events(la)[0])
        for forbidden in (secret, "Bearer abcdefghijklmnop", "diff --git", "private log output", private, "SECRET-OBJECTIVE"):
            self.assertNotIn(forbidden, blob)
        d = events(la)[0]["data"]
        for key in ("objective", "patch", "logs", "token", "bbs_test_token", "workdir"):
            self.assertNotIn(key, d)
        self.assertEqual(d["lease"]["sha256"], derived_lease_sha([], "a" * 40))

    def test_recovery_refuses_conflicting_journal_evidence_without_overwriting(self):
        tmp = tempfile.mkdtemp(prefix="landed-conflict-")
        la = self.mk(data_dir=tmp)
        a = self.add(la, aid="att_conflict01")
        la._set(a, "succeeded", "done")
        path = next(outbox(la).glob("*.json"))
        before = path.read_bytes()

        a["state"] = "failed"
        la._save(a)
        la.attempts.pop("att_conflict01")
        la2 = self.mk(data_dir=tmp)
        self.assertEqual(len(events(la2)), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(la2.landing_conflicts)


if __name__ == "__main__":
    unittest.main()
