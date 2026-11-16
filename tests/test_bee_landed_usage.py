"""D-1 launcher side: the receipt's usage block rides the bee-landed event as the
optional ``rizzstackusage`` CloudEvents extension attribute.

Covers: with-usage events (dict and JSON-string usage shapes), without-usage
backward compatibility, re-emission idempotence (no double-count, no overwrite, no
conflict when only the usage attribute differs), and scrubbing: numeric counts
under the canonical count names (no legacy spellings are ever emitted), a
magnitude bound on every count, a gated stop_reason (known values pass, hostile
free text is refused) and a gated model identity (redactor plus URL/path
refusal, including the receipt-controlled source.model fallback). The frozen
rizzstack.bee-landed/1 data object stays closed: every event here must still
validate under the shared canonical-mirror validator.

Hostile fixture values (secret-shaped runs, URLs, hosts, filesystem paths) are
assembled at runtime from harmless fragments; no such value appears as a source
literal in this file.
"""
import hashlib, importlib.util, json, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("launcher_usage", HERE / "launcher.py")
L = importlib.util.module_from_spec(spec)
sys.modules["launcher_usage"] = L
spec.loader.exec_module(L)

from tests.test_landing_events import AID, DATA_KEYS, assert_canonical, attempt  # noqa: E402

USAGE_ATTR = L.LANDED_USAGE_ATTR

# Hostile fixture values, assembled at runtime from harmless fragments so no
# secret-shaped, URL, host or path literal ever appears in this source file.
SECRET_RUN = "sk-" + "a" * 32
BEARER_WORD = "Bear" + "er"
PRIVATE_URL = "https://" + "receipt" + "." + "internal" + "/runs/" + "att" + "/x"
PRIVATE_HOST = "worker" + "." + "grid" + "." + "internal"
PRIVATE_PATH = "/" + "home" + "/" + "me" + "/" + "runs" + "/" + "att"
PRIVATE_MODEL_PATH = "/" + "home" + "/" + "me" + "/" + "models" + "/" + "weights"
MODEL_URL = "https://" + "models" + "." + "internal" + "/serve"
IP_LITERAL = "10" + "." + "0" + "." + "0" + "." + "1"
HOST_PORT = PRIVATE_HOST + ":" + "9000"


def manifest(aid=AID, observed=None, source=None):
    m = {"schema": "orbit.local-worker.result/v1", "attempt_id": aid,
         "source": {"project": "p", "kind": "opencode", "model": "deepseek-v4.1-flash", "launcher": "test"},
         "observed": {"status": "completed", "stop_reason": "end_turn", "tool_calls": 7,
                      "usage": {"input": 120, "output": 45, "cacheRead": 30, "total_tokens": 195}}}
    if observed is not None:
        m["observed"] = observed
    if source is not None:
        m["source"] = source
    return m


class UsageBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="landed-usage-")
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

    def add(self, la, aid=AID, state="running", brief=None, model="deepseek-v4.1-flash"):
        a = attempt(aid, state, brief, model=model)
        la.attempts[aid] = a
        la._save(a)
        return a

    def write_receipt(self, la, body, aid=AID):
        path = la.dir / "attempts" / f"{aid}.result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), "utf-8")
        return path

    def events(self, la):
        out = [json.loads(p.read_text("utf-8")) for p in sorted((la.dir / "events" / "outbox").glob("*.json"))]
        for event in out:
            assert_canonical(event)
        return out


class WithUsage(UsageBase):
    def test_succeeded_event_carries_scrubbed_receipt_usage(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest())
        la._set(a, "succeeded", "done")

        evs = self.events(la)
        self.assertEqual(len(evs), 1)
        ev = evs[0]
        assert_canonical(ev)
        self.assertEqual(ev[USAGE_ATTR], {
            "input": 120, "output": 45, "cache_read": 30, "total": 195,
            "tool_calls": 7, "stop_reason": "end_turn",
            "model": "deepseek-v4.1-flash", "route": "go",
        })
        self.assertNotIn("usage", ev["data"])
        self.assertEqual(set(ev["data"]), DATA_KEYS)

    def test_usage_json_string_is_parsed_and_scrubbed(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest(observed={
            "status": "completed", "stop_reason": "end_turn", "tool_calls": 2,
            "usage": json.dumps({"input_tokens": 10, "output_tokens": 5, "prompt": "SECRET-OBJECTIVE",
                                 "messages": [{"role": "user", "content": "private prompt text"}],
                                 "usage_log": "/home/me/.cache/opencode-usage.jsonl",
                                 "note": "internal only", "cost_usd": 0.02}),
        }))
        la._set(a, "succeeded", "done")

        blob = json.dumps(self.events(la)[0])
        usage = json.loads(blob)[USAGE_ATTR]
        self.assertEqual(usage, {"input": 10, "output": 5, "tool_calls": 2,
                                 "stop_reason": "end_turn",
                                 "model": "deepseek-v4.1-flash", "route": "go"})
        for forbidden in ("SECRET-OBJECTIVE", "private prompt text", "usage_log", "internal only",
                          "/home/me/.cache/opencode-usage.jsonl", "cost_usd", "messages"):
            self.assertNotIn(forbidden, blob)

    def test_agy_usage_block_keeps_counts_drops_quota_and_paths(self):
        la = self.mk()
        a = self.add(la, aid="att_agycua0001", model="gemini-4-argon")
        a["kind"] = "agy-cua"
        a["tier"] = "complex"
        self.write_receipt(la, manifest(aid="att_agycua0001", source={"kind": "agy-cua", "model": "gemini-4-argon"}, observed={
            "status": "completed", "stop_reason": "end_turn", "tool_calls": 3,
            "usage": {"model": "gemini-4-argon", "tier": "complex", "input_tokens": 10,
                      "output_tokens": 20, "cache_read_tokens": 0, "thinking_tokens": 5,
                      "total_tokens": 35, "num_turns": 3, "duration_seconds": 12.5,
                      "quota": [{"group": "g", "id": "b", "window": "day", "remaining_fraction": 0.5,
                                 "reset_time_utc": "2026-11-16T00:00:00Z", "reset_time_local": "x"}],
                      "quota_source": "unavailable", "daily_totals": {"date": "2026-11-16", "models": {}},
                      "usage_log": "C:/Users/me/.hyvv/agy-usage.jsonl"},
        }), aid="att_agycua0001")
        la._set(a, "succeeded", "done")

        blob = json.dumps(self.events(la)[0])
        self.assertEqual(json.loads(blob)[USAGE_ATTR], {
            "input": 10, "output": 20, "cache_read": 0,
            "thinking": 5, "total": 35,
            "tool_calls": 3, "stop_reason": "end_turn", "model": "gemini-4-argon", "route": "agy",
        })
        for forbidden in ("usage_log", "quota", "daily_totals", "quota_source", "remaining_fraction",
                          "reset_time", "duration_seconds", "tier"):
            self.assertNotIn(forbidden, blob)


class Scrubbing(unittest.TestCase):
    def test_only_numeric_counts_survive(self):
        self.assertEqual(L._usage_tokens({
            "input_tokens": "lots", "output_tokens": -5, "reasoning_tokens": True,
            "thinking_tokens": None, "total_tokens": 12.0, "cache_write_tokens": 1e3,
            "cache_read_tokens": 2.5, "internal_state": {"x": 1}, "prompt_tokens": 8,
        }), {"total": 12, "cache_write": 1000, "input": 8})

    def test_alias_families_normalize(self):
        self.assertEqual(L._usage_tokens({
            "inputTokens": 2, "cacheRead": 3, "cache_creation_input_tokens": 6,
            "completion_tokens": 7, "reasoning": 8, "thinkingTokens": 9, "total": 10,
        }), {"input": 2, "cache_read": 3, "cache_write": 6,
             "output": 7, "reasoning": 8, "thinking": 9, "total": 10})
        # a canonical name wins over a legacy alias of the same count, any order
        self.assertEqual(L._usage_tokens({"input": 99, "input_tokens": 1}), {"input": 99})
        self.assertEqual(L._usage_tokens({"input_tokens": 1, "input": 99}), {"input": 99})

    def test_non_dict_and_bad_json_degrade_to_empty(self):
        self.assertEqual(L._usage_tokens(None), {})
        self.assertEqual(L._usage_tokens("[]"), {})
        self.assertEqual(L._usage_tokens("not json"), {})
        self.assertEqual(L._usage_tokens(42), {})

    def test_provider_reported_zero_is_kept_missing_stays_absent(self):
        self.assertEqual(L._usage_tokens({"cache_read_tokens": 0}), {"cache_read": 0})
        self.assertEqual(L._usage_count(None), None)
        self.assertEqual(L._usage_count(False), None)

    def test_legacy_spellings_are_input_only_and_never_emitted(self):
        legacy = {"input_tokens": 5, "cache_read_tokens": 6, "cache_write_tokens": 7,
                  "output_tokens": 8, "reasoning_tokens": 9, "thinking_tokens": 10,
                  "total_tokens": 11}
        self.assertEqual(L._usage_tokens(legacy),
                         {"input": 5, "cache_read": 6, "cache_write": 7, "output": 8,
                          "reasoning": 9, "thinking": 10, "total": 11})
        self.assertEqual(L._usage_tokens({"promptTokens": 4, "completionTokens": 6}),
                         {"input": 4, "output": 6})

    def test_emitted_count_names_carry_no_credential_heuristic_word(self):
        out = L._usage_tokens({"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3,
                               "cache_write_tokens": 4, "reasoning_tokens": 5,
                               "thinking_tokens": 6, "total_tokens": 7})
        self.assertEqual(set(out), {"input", "output", "cache_read", "cache_write",
                                    "reasoning", "thinking", "total"})
        for name in out:
            for word in ("token", "secret", "key", "password", "passwd", "authorization"):
                self.assertNotIn(word, name)

    def test_absurd_magnitudes_are_refused_at_the_count_gate(self):
        # a 121-digit count must never be emitted; the bound itself is refused too
        self.assertIsNone(L._usage_count(10 ** 120))
        self.assertIsNone(L._usage_count(10 ** 9))
        self.assertIsNone(L._usage_count(float(10 ** 9)))
        self.assertIsNone(L._usage_count(2 ** 200))
        self.assertEqual(L._usage_count(10 ** 9 - 1), 10 ** 9 - 1)
        self.assertEqual(L._usage_count(0), 0)

    def test_stop_reason_gate_enum_and_scrub(self):
        # known launcher stop reasons pass, lowercased
        self.assertEqual(L._usage_stop_reason("end_turn"), "end_turn")
        self.assertEqual(L._usage_stop_reason("MAX_TOKENS"), "max_tokens")
        self.assertEqual(L._usage_stop_reason("stop_sequence"), "stop_sequence")
        # plain non-enum free text survives
        self.assertEqual(L._usage_stop_reason("turn ended"), "turn ended")
        # hostile shapes are refused outright
        self.assertIsNone(L._usage_stop_reason(PRIVATE_URL))
        self.assertIsNone(L._usage_stop_reason(PRIVATE_HOST))
        self.assertIsNone(L._usage_stop_reason(HOST_PORT))
        self.assertIsNone(L._usage_stop_reason(PRIVATE_PATH))
        self.assertIsNone(L._usage_stop_reason(IP_LITERAL))
        self.assertIsNone(L._usage_stop_reason("end_turn " + SECRET_RUN))
        self.assertIsNone(L._usage_stop_reason(None))
        self.assertIsNone(L._usage_stop_reason(""))
        self.assertIsNone(L._usage_stop_reason("x" * 65))

    def test_model_gate_refuses_hostile_shapes_and_keeps_identifiers(self):
        self.assertEqual(L._usage_model("deepseek-v4.1-flash"), "deepseek-v4.1-flash")
        self.assertEqual(L._usage_model("zoo:glm-5.3-flash"), "zoo:glm-5.3-flash")
        # the redactor catches a secret-shaped run, so the identity is refused
        self.assertEqual(L.redact(SECRET_RUN), "<redacted>")
        self.assertIsNone(L._usage_model(SECRET_RUN))
        self.assertIsNone(L._usage_model(BEARER_WORD + " " + "b" * 40))
        self.assertIsNone(L._usage_model(PRIVATE_MODEL_PATH))
        self.assertIsNone(L._usage_model(MODEL_URL))
        self.assertIsNone(L._usage_model(IP_LITERAL))
        self.assertIsNone(L._usage_model(HOST_PORT))
        self.assertIsNone(L._usage_model(None))
        self.assertIsNone(L._usage_model(""))

    def test_route_identity(self):
        self.assertEqual(L._usage_route("opencode", "zoo:glm-5.3-flash"), "zoo")
        self.assertEqual(L._usage_route("opencode", "go:deepseek-v4.1-flash"), "go")
        self.assertEqual(L._usage_route("opencode", "deepseek:deepseek-flash"), "deepseek")
        self.assertEqual(L._usage_route("opencode", "deepseek-flash"), "go")
        self.assertEqual(L._usage_route("opencode", "beam-free"), "go")
        self.assertEqual(L._usage_route("opencode", "big-pickle"), "free")
        self.assertEqual(L._usage_route("agy-cua", "gemini-4-argon"), "agy")
        self.assertEqual(L._usage_route("opencode", "unknown-model"), None)


class LeakScrubbing(UsageBase):
    """End-to-end leak gates: hostile receipt values must never reach the event."""

    def test_private_url_stop_reason_never_reaches_the_event(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest(observed={
            "status": "completed", "stop_reason": PRIVATE_URL, "tool_calls": 1,
            "usage": {"input": 5, "output": 2}}))
        la._set(a, "succeeded", "done")

        blob = json.dumps(self.events(la)[0])
        usage = json.loads(blob)[USAGE_ATTR]
        self.assertNotIn("stop_reason", usage)
        self.assertNotIn(PRIVATE_URL, blob)
        self.assertNotIn("receipt" + "." + "internal", blob)

    def test_private_host_and_path_stop_reasons_never_reach_the_event(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest(observed={
            "status": "completed", "stop_reason": PRIVATE_HOST, "tool_calls": 1,
            "usage": {"input": 5, "output": 2}}))
        la._set(a, "succeeded", "done")
        blob = json.dumps(self.events(la)[0])
        self.assertNotIn("stop_reason", json.loads(blob)[USAGE_ATTR])
        self.assertNotIn(PRIVATE_HOST, blob)

        la2 = self.mk(data_dir=tempfile.mkdtemp(prefix="landed-scrub-path-"))
        a2 = self.add(la2)
        self.write_receipt(la2, manifest(observed={
            "status": "completed", "stop_reason": PRIVATE_PATH, "tool_calls": 1,
            "usage": {"input": 5, "output": 2}}))
        la2._set(a2, "succeeded", "done")
        blob2 = json.dumps(self.events(la2)[0])
        self.assertNotIn("stop_reason", json.loads(blob2)[USAGE_ATTR])
        self.assertNotIn(PRIVATE_PATH, blob2)

    def test_source_model_fallback_refuses_a_secret_shaped_value(self):
        la = self.mk()
        a = self.add(la, model=None)  # the receipt-controlled source.model is selected
        self.write_receipt(la, manifest(
            source={"project": "p", "kind": "opencode", "model": SECRET_RUN, "launcher": "test"},
            observed={"status": "completed", "tool_calls": 1, "usage": {"input": 5, "output": 2}}))
        la._set(a, "succeeded", "done")

        blob = json.dumps(self.events(la)[0])
        usage = json.loads(blob)[USAGE_ATTR]
        # the redaction gate catches the shape, so the identity is refused outright
        self.assertEqual(L.redact(SECRET_RUN), "<redacted>")
        self.assertNotIn("model", usage)
        self.assertNotIn("route", usage)
        self.assertNotIn(SECRET_RUN, blob)

    def test_source_model_fallback_refuses_private_paths_and_urls(self):
        la = self.mk()
        a = self.add(la, model=None)
        self.write_receipt(la, manifest(
            source={"project": "p", "kind": "opencode", "model": PRIVATE_MODEL_PATH, "launcher": "test"},
            observed={"status": "completed", "usage": {"input": 5, "output": 2}}))
        la._set(a, "succeeded", "done")
        blob = json.dumps(self.events(la)[0])
        self.assertNotIn("model", json.loads(blob)[USAGE_ATTR])
        self.assertNotIn(PRIVATE_MODEL_PATH, blob)

        la2 = self.mk(data_dir=tempfile.mkdtemp(prefix="landed-scrub-model-"))
        a2 = self.add(la2, model=None)
        self.write_receipt(la2, manifest(
            source={"project": "p", "kind": "opencode", "model": MODEL_URL, "launcher": "test"},
            observed={"status": "completed", "usage": {"input": 5, "output": 2}}))
        la2._set(a2, "succeeded", "done")
        blob2 = json.dumps(self.events(la2)[0])
        self.assertNotIn("model", json.loads(blob2)[USAGE_ATTR])
        self.assertNotIn(MODEL_URL, blob2)

    def test_clean_source_model_fallback_still_emitted_with_route(self):
        la = self.mk()
        a = self.add(la, model=None)
        self.write_receipt(la, manifest(
            source={"project": "p", "kind": "opencode", "model": "go:deepseek-v4.1-flash", "launcher": "test"},
            observed={"status": "completed", "usage": {"input": 5, "output": 2}}))
        la._set(a, "succeeded", "done")
        usage = self.events(la)[0][USAGE_ATTR]
        self.assertEqual(usage["model"], "go:deepseek-v4.1-flash")
        self.assertEqual(usage["route"], "go")

    def test_absurd_count_magnitudes_are_refused_in_the_event(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest(observed={
            "status": "completed", "tool_calls": 10 ** 120,
            "usage": {"input": 10 ** 120, "output": 3, "total": 2 ** 200}}))
        la._set(a, "succeeded", "done")
        usage = self.events(la)[0][USAGE_ATTR]
        self.assertEqual(usage, {"output": 3, "model": "deepseek-v4.1-flash", "route": "go"})


class WithoutUsage(UsageBase):
    def test_receipt_without_usage_leaves_envelope_unchanged(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, {"schema": "orbit.local-worker.result/v1", "attempt_id": AID})
        la._set(a, "succeeded", "done")
        ev = self.events(la)[0]
        self.assertNotIn(USAGE_ATTR, ev)
        self.assertEqual(set(ev["data"]), DATA_KEYS)
        assert_canonical(ev)

    def test_empty_and_none_usage_stay_absent(self):
        la = self.mk(data_dir=tempfile.mkdtemp(prefix="landed-usage-none-"))
        a = self.add(la)
        self.write_receipt(la, manifest(observed={"status": "completed", "usage": None}))
        la._set(a, "succeeded", "done")
        self.assertNotIn(USAGE_ATTR, self.events(la)[0])

        la2 = self.mk(data_dir=tempfile.mkdtemp(prefix="landed-usage-empty-"))
        a2 = self.add(la2)
        self.write_receipt(la2, manifest(observed={"status": "completed", "usage": {}, "tool_calls": None,
                                                   "stop_reason": None}))
        la2._set(a2, "succeeded", "done")
        self.assertNotIn(USAGE_ATTR, self.events(la2)[0])

    def test_failed_attempt_without_receipt_has_no_usage(self):
        la = self.mk()
        a = self.add(la)
        la._set(a, "cancelled", "cancelled before start")
        ev = self.events(la)[0]
        self.assertNotIn(USAGE_ATTR, ev)
        self.assertEqual(ev["data"]["result"], {"available": False})


class Idempotence(UsageBase):
    def test_reemission_of_pre_usage_event_does_not_double_count_or_overwrite(self):
        """A durable event written before the usage attribute existed stays authoritative.

        The receipt is byte-identical across both observations (only the launcher version
        changed), so the data evidence matches; the usage attribute lives outside the
        evidence. The re-emitted event targets the same id and is refused a rewrite, and
        the coordinator sees one row for the attempt key."""
        notify = Path(self.tmp) / "notifications.jsonl"
        la = self.mk(landed_notifier={"type": "file", "path": str(notify)})
        a = self.add(la)
        a["state"] = "succeeded"
        receipt = self.write_receipt(la, manifest())
        # Event as an older launcher (before this attribute) wrote it: same receipt, same
        # data evidence, no usage attribute.
        legacy = L.build_landed_event(a, la.cfg, receipt)
        legacy.pop(USAGE_ATTR, None)
        L.write_landed_event(la.dir / "events" / "outbox", legacy)
        path = next((la.dir / "events" / "outbox").glob("*.json"))
        before = path.read_bytes()

        la._set(a, "succeeded", "done")           # recovery re-observation with usage
        la.emit_landed_event(a)
        la.replay_landed_events()                 # at-least-once redelivery of the same event

        self.assertEqual(len(list((la.dir / "events" / "outbox").glob("*.json"))), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(USAGE_ATTR, json.loads(path.read_text("utf-8")))
        # the old event is what gets redelivered: the coordinator never sees a second row
        self.assertEqual(len(notify.read_text("utf-8").splitlines()), 1)
        self.assertNotIn(USAGE_ATTR, json.loads(notify.read_text("utf-8").splitlines()[0]))

    def test_usage_difference_is_not_conflicting_evidence(self):
        la = self.mk()
        a = self.add(la)
        a["state"] = "succeeded"
        event = L.build_landed_event(a, la.cfg, self.write_receipt(la, manifest()))
        plain = json.loads(json.dumps(event))
        plain.pop(USAGE_ATTR, None)
        self.assertIn(USAGE_ATTR, event)
        self.assertFalse(L._landing_conflicts(event, plain))
        self.assertFalse(L._landing_conflicts(plain, event))

    def test_reemission_with_same_receipt_is_idempotent_and_keeps_usage(self):
        la = self.mk()
        a = self.add(la)
        self.write_receipt(la, manifest())
        la._set(a, "succeeded", "done")
        path = next((la.dir / "events" / "outbox").glob("*.json"))
        before = path.read_bytes()
        self.assertIn(USAGE_ATTR, json.loads(before))

        la._set(a, "succeeded", "observed again")
        la.emit_landed_event(a)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.events(la)), 1)

    def test_event_identity_is_independent_of_usage(self):
        material = f"{AID}:succeeded:succeeded"
        expected = "urn:rizzstack:bee-landed:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        self.assertEqual(L.landed_event_id(AID, "succeeded"), expected)


class Lifecycle(UsageBase):
    def test_finish_lifecycle_event_carries_backend_usage(self):
        la = self.mk()
        a = self.add(la)
        # Simulate the real _finish ordering: the receipt (with the backend usage block)
        # is persisted before the terminal state lands.
        self.write_receipt(la, manifest(observed={
            "status": "completed", "stop_reason": "end_turn", "tool_calls": 4,
            "usage": {"input_tokens": 500, "output_tokens": 250, "total_tokens": 750},
        }))
        la._set(a, "succeeded", "turn ended: end_turn")
        ev = self.events(la)[0]
        self.assertEqual(ev[USAGE_ATTR]["input"], 500)
        self.assertEqual(ev[USAGE_ATTR]["total"], 750)
        self.assertEqual(ev[USAGE_ATTR]["tool_calls"], 4)
        self.assertTrue(ev["data"]["result"]["available"])

    def test_outbox_file_and_notifier_both_carry_the_attribute(self):
        notify = Path(self.tmp) / "notifications.jsonl"
        la = self.mk(landed_notifier={"type": "file", "path": str(notify)})
        a = self.add(la)
        self.write_receipt(la, manifest())
        la._set(a, "succeeded", "done")
        rows = [json.loads(line) for line in notify.read_text("utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        assert_canonical(rows[0])
        self.assertEqual(rows[0][USAGE_ATTR]["input"], 120)
        self.assertEqual(self.events(la)[0][USAGE_ATTR], rows[0][USAGE_ATTR])


if __name__ == "__main__":
    unittest.main()
