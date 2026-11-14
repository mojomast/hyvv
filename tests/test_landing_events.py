"""Durable rizzstack.bee.landed.v1 terminal events: one per attempt, replayable outbox."""
import hashlib, importlib.util, json, socket, sys, tempfile, threading, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("launcher_landing", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher_landing"] = L
spec.loader.exec_module(L)

AID = "att_landing0001"


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
    return [json.loads(p.read_text("utf-8")) for p in sorted(outbox(la).glob("*.json"))]


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

    def test_succeeded_writes_one_cloudevent_with_evidence(self):
        la = self.mk()
        a = self.add(la)
        result = write_result(la)
        la._set(a, "succeeded", "done")

        evs = events(la)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        self.assertEqual(ev["specversion"], "1.0")
        self.assertEqual(ev["type"], "rizzstack.bee.landed.v1")
        self.assertEqual(ev["subject"], AID)
        self.assertEqual(ev["source"], "hyvv://worker-1")
        self.assertTrue(ev["time"].endswith("Z"))
        self.assertEqual(ev["id"], next(outbox(la).glob("*.json")).stem)

        d = ev["data"]
        self.assertEqual(d["event_id"], ev["id"])
        self.assertEqual((d["feature_id"], d["job_id"], d["task_id"], d["attempt_id"]),
                         ("feat_1", "job_1", "task_1", AID))
        self.assertEqual(d["coordinator"], {"id": "orbit", "session": "sess_1"})
        self.assertEqual((d["harness"], d["model"], d["state"]), ("opencode", "deepseek-v4.1-flash", "succeeded"))
        self.assertEqual(d["source_rev"], "a" * 40)
        self.assertEqual(d["lease_sha256"], "b" * 64)
        self.assertTrue(d["result"]["available"])
        self.assertEqual(d["result"]["artifact_sha256"], hashlib.sha256(result.read_bytes()).hexdigest())
        self.assertEqual(d["result"]["reference"], f"hyvv://attempts/{AID}/result")
        self.assertEqual(d["result"]["schema"], "orbit.local-worker.result/v1")

    def test_finish_lifecycle_writes_event_after_result(self):
        la = self.mk()
        (Path(self.tmp) / "go.key").write_text("fake-provider-key", "utf-8")
        thread = threading.Thread(target=la.loop.run_forever, daemon=True)
        thread.start()
        try:
            aid = "att_lifecycle01"
            la.submit(aid, {"objective": "x"})
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
        self.assertEqual(ev["data"]["result"], {"available": False, "artifact_sha256": None, "reference": None})

    def test_cancelled_event_is_emitted(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "cancelled", "interrupt confirmed")
        ev = events(la)[0]
        self.assertEqual(ev["data"]["state"], "cancelled")
        self.assertFalse(ev["data"]["result"]["available"])

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
        self.assertEqual(d["lease_sha256"], hashlib.sha256(L._canonical_bytes({"path": private, "id": "lease_1"})).hexdigest())

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
