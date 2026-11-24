"""Approved tests: harvest per-request and per-tool metrics from OpenCode session messages.

hyvv.metrics_harvest turns assistant messages from an OpenCode session (the
1.x export shape: {"info": ..., "parts": [...]}, and the 2.x session_message
shape) into gen_ai.inference/v1 and gen_ai.tool_call/v1 rows, as frozen in
mojomast/workflow observability/schemas (copied into tests/fixtures/metrics).

Counts, timings and bounded labels only. No prompt, completion, reasoning text,
tool input, tool output, command, path or title may appear in any row.
"""

import json
import re
import unittest
from pathlib import Path

from hyvv import metrics_harvest as mh

FIX = Path(__file__).resolve().parent / "fixtures" / "metrics"
SECRET = "PLANTED-PROMPT-TEXT-9f3a"
JOIN = {"attempt_id": "att_0123abcd", "card_id": "card-1", "run_id": "run-1", "project": "example",
        "role": "work", "task_id": "task-1", "job_id": None, "trace_id": None}
T0 = 1795000000000  # ms epoch, 2026-11-18T...Z


def schema(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def v1_messages():
    """Two assistant steps in the OpenCode 1.x export shape (mock data)."""
    return [
        {"info": {"id": "msg_u1", "role": "user", "time": {"created": T0 - 1000}},
         "parts": [{"type": "text", "text": SECRET}]},
        {"info": {"id": "msg_a1", "role": "assistant", "modelID": "deepseek/deepseek-v4.1-flash-1017", "providerID": "nous",
                  "time": {"created": T0, "completed": T0 + 12000}, "finish": "tool-calls", "cost": 0,
                  "tokens": {"input": 1800, "output": 600, "reasoning": 120, "cache": {"read": 12000, "write": 0}},
                  "system": [SECRET], "path": {"cwd": "/home/someone/repo"}},
         "parts": [
             {"type": "step-start", "time": {"start": T0}},
             {"type": "reasoning", "text": SECRET, "time": {"start": T0 + 2000, "end": T0 + 3000}},
             {"type": "text", "text": SECRET, "time": {"start": T0 + 3000, "end": T0 + 4000}},
             {"type": "tool", "tool": "bash", "callID": "call_1",
              "state": {"status": "completed", "input": {"command": SECRET}, "output": SECRET * 10, "title": SECRET,
                        "time": {"start": T0 + 4000, "end": T0 + 11000}}},
             {"type": "tool", "tool": "Github_create_issue", "callID": "call_2",
              "state": {"status": "error", "input": {"x": SECRET}, "error": SECRET, "time": {"start": T0 + 11000, "end": T0 + 11500}}},
             {"type": "step-finish", "reason": "tool-calls", "time": {"start": T0 + 12000}},
         ]},
        {"info": {"id": "msg_a2", "role": "assistant", "modelID": "deepseek/deepseek-v4.1-flash-1017", "providerID": "nous",
                  "time": {"created": T0 + 13000}, "tokens": None},
         "parts": []},
    ]


def v2_messages():
    """Two assistant rows in the OpenCode 2.x session_message shape (mock data)."""
    return [
        {"type": "user", "data": {"time": {"created": T0 - 1000}, "text": SECRET}},
        {"type": "assistant", "data": {
            "time": {"created": T0, "streamed": T0 + 1500, "completed": T0 + 9500},
            "agent": "build", "model": {"id": "kimi-for-coding", "providerID": "kimi", "variant": "default"},
            "finish": "stop", "cost": 0,
            "tokens": {"input": 4000, "output": 800, "reasoning": 0, "cache": {"read": 0, "write": 50}},
            "content": [
                {"type": "reasoning", "text": SECRET, "time": {"created": T0 + 1500, "completed": T0 + 2000}},
                {"type": "tool", "id": "call_9", "name": "edit", "executed": True,
                 "state": {"status": "completed", "input": {"filePath": SECRET}, "content": [SECRET], "metadata": {"diff": SECRET}},
                 "time": {"created": T0 + 2000, "ran": T0 + 2100, "completed": T0 + 2600}},
                {"type": "text", "text": SECRET},
            ]}},
        {"type": "idle", "data": {"time": {"created": T0 + 10000}, "outcome": "done"}},
    ]


COMMON_DEFS = json.loads((FIX / "common.schema.json").read_text(encoding="utf-8")).get("$defs", {})


def _type_ok(value, sch):
    types = sch.get("type")
    if types is None:
        return True
    types = [types] if isinstance(types, str) else types
    kinds = {"null": value is None, "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    return any(kinds[t] for t in types)


def _pattern_ok(value, sch):
    if not isinstance(value, str) or "pattern" not in sch:
        return True
    return bool(re.search(sch["pattern"], value))


def _bounds_ok(value, sch):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    if "minimum" in sch and value < sch["minimum"]:
        return False
    if "maximum" in sch and value > sch["maximum"]:
        return False
    return True


def conforms(value, sch):
    """Tiny checker for the subset of JSON Schema these schemas use."""
    if "$ref" in sch:
        return conforms(value, COMMON_DEFS[sch["$ref"].split("/")[-1]])
    if "oneOf" in sch:
        return sum(conforms(value, s) for s in sch["oneOf"]) == 1
    if "anyOf" in sch:
        return any(conforms(value, s) for s in sch["anyOf"])
    if "const" in sch and value != sch["const"]:
        return False
    if "enum" in sch and value not in sch["enum"]:
        return False
    if not _type_ok(value, sch):
        return False
    if not _pattern_ok(value, sch):
        return False
    return _bounds_ok(value, sch)


def check_row(test, row, schema_doc):
    props = schema_doc["properties"]
    test.assertTrue(set(row) <= set(props), sorted(set(row) - set(props)))
    for key in schema_doc["required"]:
        test.assertIn(key, row)
    for key, value in row.items():
        test.assertTrue(conforms(value, props[key]), f"{key}={value!r}")
    test.assertEqual(row["schema"], props["schema"]["const"])
    test.assertEqual(row["schema_version"], "v1")


class HarvestV1(unittest.TestCase):
    def setUp(self):
        self.out = mh.harvest(v1_messages(), join=JOIN, route="nous", requested_model="nous:deepseek-v4.1-flash-1017",
                              harness="opencode", context_limit=128000)

    def test_one_inference_row_per_assistant_message_in_order(self):
        rows = self.out["inference"]
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        for r in rows:
            check_row(self, r, schema("inference.schema.json"))
            for k, v in JOIN.items():
                self.assertEqual(r[k], v)

    def test_timings_and_ttft_from_part_timing(self):
        r = self.out["inference"][0]
        self.assertEqual(r["ts_start"], "2026-11-18T11:06:40Z")
        self.assertEqual(r["ts_end"], "2026-11-18T11:06:52Z")
        self.assertAlmostEqual(r["duration_s"], 12.0)
        self.assertAlmostEqual(r["ttft_s"], 2.0)
        self.assertEqual(r["ttft_source"], "harness_part_timing")
        self.assertAlmostEqual(r["output_tps"], 600 / 10.0)

    def test_token_counts_and_labels(self):
        r = self.out["inference"][0]
        self.assertEqual((r["input_tokens"], r["output_tokens"], r["reasoning_tokens"], r["cache_read_tokens"], r["cache_write_tokens"]),
                         (1800, 600, 120, 12000, 0))
        self.assertEqual(r["usage_status"], "known")
        self.assertEqual(r["provider"], "nous")
        self.assertEqual(r["route"], "nous")
        self.assertEqual(r["harness"], "opencode")
        self.assertEqual(r["requested_model"], "nous:deepseek-v4.1-flash-1017")
        self.assertEqual(r["served_model"], "deepseek/deepseek-v4.1-flash-1017")
        self.assertEqual(r["finish_reason"], "tool-calls")
        self.assertAlmostEqual(r["context_fill_ratio"], (1800 + 12000 + 0) / 128000, places=6)
        self.assertEqual(r["context_limit"], 128000)
        self.assertIsNone(r["cost_usd_list"])
        self.assertEqual(r["cost_status"], "unknown")

    def test_unknown_usage_stays_null_never_zero(self):
        r = self.out["inference"][1]
        for k in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
                  "ttft_s", "duration_s", "output_tps", "ts_end", "context_fill_ratio"):
            self.assertIsNone(r[k], k)
        self.assertEqual(r["usage_status"], "unknown")
        self.assertEqual(r["ttft_source"], "unknown")

    def test_tool_rows(self):
        rows = self.out["tools"]
        self.assertEqual([r["call_seq"] for r in rows], [1, 2])
        for r in rows:
            check_row(self, r, schema("tool-call.schema.json"))
        bash, other = rows
        self.assertEqual((bash["tool_name"], bash["status"], bash["step_seq"]), ("bash", "ok", 1))
        self.assertAlmostEqual(bash["duration_s"], 7.0)
        self.assertEqual(bash["output_bytes"], len((SECRET * 10).encode("utf-8")))
        self.assertEqual((other["tool_name"], other["status"]), ("other", "error"))
        self.assertNotIn(SECRET, json.dumps(other))


class HarvestV2(unittest.TestCase):
    def setUp(self):
        self.out = mh.harvest(v2_messages(), join=JOIN, route="kimi", requested_model="kimi-for-coding", harness="opencode")

    def test_streamed_time_is_first_chunk(self):
        rows = self.out["inference"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        check_row(self, r, schema("inference.schema.json"))
        self.assertAlmostEqual(r["ttft_s"], 1.5)
        self.assertAlmostEqual(r["duration_s"], 9.5)
        self.assertAlmostEqual(r["output_tps"], 800 / 8.0)
        self.assertEqual((r["provider"], r["served_model"], r["finish_reason"]), ("kimi", "kimi-for-coding", "stop"))
        self.assertEqual(r["cache_write_tokens"], 50)
        self.assertIsNone(r["context_limit"])
        self.assertIsNone(r["context_fill_ratio"])

    def test_v2_tool_timing_uses_ran_to_completed(self):
        (t,) = self.out["tools"]
        check_row(self, t, schema("tool-call.schema.json"))
        self.assertEqual((t["tool_name"], t["status"], t["step_seq"]), ("edit", "ok", 1))
        self.assertAlmostEqual(t["duration_s"], 0.5)


class Privacy(unittest.TestCase):
    def test_no_content_leaks_in_any_row(self):
        for msgs in (v1_messages(), v2_messages()):
            out = mh.harvest(msgs, join=JOIN, route="nous", requested_model=None, harness="opencode")
            text = json.dumps(out)
            self.assertNotIn(SECRET, text)
            self.assertNotIn("/home/", text)

    def test_labels_are_bounded(self):
        out = mh.harvest(v1_messages(), join=JOIN, route="Nous Portal!", requested_model="https://x.example/m", harness="opencode")
        r = out["inference"][0]
        self.assertRegex(r["provider"], r"^[a-z0-9][a-z0-9._-]{0,63}$")
        self.assertIsNone(r["requested_model"])
        for t in out["tools"]:
            self.assertRegex(t["tool_name"], r"^[a-z0-9][a-z0-9_.:-]{0,63}$")

    def test_tool_vocabulary_is_closed(self):
        self.assertIn("bash", mh.KNOWN_TOOLS)
        self.assertIn("edit", mh.KNOWN_TOOLS)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,63}", n) for n in mh.KNOWN_TOOLS))
        self.assertEqual(mh.tool_label("Bash"), "bash")
        self.assertEqual(mh.tool_label("totally_new_tool"), "other")


class Robustness(unittest.TestCase):
    def test_garbage_input_never_raises(self):
        for bad in (None, [], [None, 1, "x", {}], [{"info": "nope"}], [{"type": "assistant", "data": None}],
                    [{"info": {"role": "assistant", "time": {"created": "soon"}}, "parts": "x"}]):
            out = mh.harvest(bad, join=JOIN, route="go", requested_model=None, harness="opencode")
            self.assertEqual(set(out), {"inference", "tools"})
            for r in out["inference"]:
                check_row(self, r, schema("inference.schema.json"))

    def test_messages_without_start_time_are_skipped(self):
        msgs = [{"info": {"role": "assistant", "time": {}, "tokens": {"input": 1}}, "parts": []}]
        self.assertEqual(mh.harvest(msgs, join=JOIN, route="go", requested_model=None, harness="opencode")["inference"], [])

    def test_negative_or_bool_counts_become_null(self):
        msgs = v1_messages()
        msgs[1]["info"]["tokens"] = {"input": -5, "output": True, "reasoning": 3.7, "cache": {"read": "9", "write": None}}
        r = mh.harvest(msgs, join=JOIN, route="nous", requested_model=None, harness="opencode")["inference"][0]
        self.assertIsNone(r["input_tokens"])
        self.assertIsNone(r["output_tokens"])
        self.assertEqual(r["usage_status"], "partial")


if __name__ == "__main__":
    unittest.main()
