"""Approved tests: bee container resource samples (resource.sample/v1).

hyvv.resource_sampler parses `docker stats --no-stream --format '{{json .}}'`
lines and `docker inspect` State objects into resource.sample/v1 rows, as
frozen in mojomast/workflow observability/schemas (copied into
tests/fixtures/metrics), and rolls one attempt's samples up into a single
attempt_rollup row. It is pure: it never runs docker itself.
"""

import json
import re
import unittest
from pathlib import Path

from hyvv import resource_sampler as rs

FIX = Path(__file__).resolve().parent / "fixtures" / "metrics"
SCHEMA = json.loads((FIX / "resource-sample.schema.json").read_text(encoding="utf-8"))
COMMON_DEFS = json.loads((FIX / "common.schema.json").read_text(encoding="utf-8")).get("$defs", {})
GiB = 1024 ** 3
MiB = 1024 ** 2


def conforms(value, sch):
    if "$ref" in sch:
        return conforms(value, COMMON_DEFS[sch["$ref"].split("/")[-1]])
    if "oneOf" in sch:
        return sum(conforms(value, s) for s in sch["oneOf"]) == 1
    if "const" in sch and value != sch["const"]:
        return False
    if "enum" in sch and value not in sch["enum"]:
        return False
    types = sch.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        kinds = {"null": value is None, "string": isinstance(value, str), "boolean": isinstance(value, bool),
                 "integer": isinstance(value, int) and not isinstance(value, bool),
                 "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
        if not any(kinds[t] for t in types):
            return False
    if isinstance(value, str) and "pattern" in sch and not re.search(sch["pattern"], value):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in sch and value < sch["minimum"]:
            return False
        if "maximum" in sch and value > sch["maximum"]:
            return False
    return True


def assert_valid(test, row):
    props = SCHEMA["properties"]
    test.assertTrue(set(row) <= set(props), sorted(set(row) - set(props)))
    for key in SCHEMA["required"]:
        test.assertIn(key, row)
    for key, value in row.items():
        test.assertTrue(conforms(value, props[key]), f"{key}={value!r}")


def stats_line(**over):
    d = {"BlockIO": "0B / 0B", "CPUPerc": "85.00%", "Container": "0123456789ab", "ID": "0123456789ab",
         "MemPerc": "30.00%", "MemUsage": "1.2GiB / 4GiB", "Name": "bee-att_mock01", "NetIO": "1MB / 256kB", "PIDs": "41"}
    d.update(over)
    return json.dumps(d)


class ParseSize(unittest.TestCase):
    def test_binary_and_decimal_units(self):
        cases = {"0B": 0, "512B": 512, "1KiB": 1024, "1.5MiB": int(1.5 * MiB), "1.2GiB": round(1.2 * GiB), "4GiB": 4 * GiB,
                 "1TiB": 1024 ** 4, "1kB": 1000, "256kB": 256000, "1KB": 1000, "1MB": 10 ** 6, "2.5GB": 2_500_000_000}
        for text, want in cases.items():
            self.assertEqual(rs.parse_size(text), want, text)

    def test_unparseable_is_none(self):
        for text in ("--", "", None, "lots", "-1MB", "1XB", 12):
            self.assertIsNone(rs.parse_size(text), text)


class ContainerSample(unittest.TestCase):
    def row(self, line=None, **kw):
        args = dict(ts="2026-11-24T20:10:20Z", host_alias="brrrrrrrr", attempt_id="att_mock01", project="example-repo", cpu_limit=2)
        args.update(kw)
        return rs.parse_stats_line(line or stats_line(), **args)

    def test_docker_stats_line_becomes_a_valid_container_row(self):
        r = self.row()
        assert_valid(self, r)
        self.assertEqual((r["schema"], r["schema_version"], r["scope"], r["source"]), ("resource.sample/v1", "v1", "container", "docker_stats"))
        self.assertEqual(r["mem_bytes"], round(1.2 * GiB))
        self.assertEqual(r["mem_limit_bytes"], 4 * GiB)
        self.assertAlmostEqual(r["cpu_ratio"], 0.85)
        self.assertEqual(r["cpu_limit"], 2)
        self.assertEqual(r["pids"], 41)
        self.assertEqual((r["net_rx_bytes"], r["net_tx_bytes"]), (10 ** 6, 256000))
        self.assertEqual(r["container_label"], "bee-att_mock01")
        self.assertEqual((r["attempt_id"], r["project"], r["host_alias"], r["ts"]), ("att_mock01", "example-repo", "brrrrrrrr", "2026-11-24T20:10:20Z"))
        self.assertIsNone(r["oom_killed"])
        self.assertIsNone(r["exit_code"])

    def test_cpu_over_one_core(self):
        self.assertAlmostEqual(self.row(stats_line(CPUPerc="187.5%"))["cpu_ratio"], 1.875)

    def test_missing_or_bad_fields_become_null(self):
        r = self.row(stats_line(MemUsage="--", CPUPerc="n/a", PIDs="-", NetIO="garbage"))
        assert_valid(self, r)
        for k in ("mem_bytes", "mem_limit_bytes", "cpu_ratio", "pids", "net_rx_bytes", "net_tx_bytes"):
            self.assertIsNone(r[k], k)

    def test_unsafe_container_name_is_dropped(self):
        r = self.row(stats_line(Name="/weird name/with spaces"))
        assert_valid(self, r)
        self.assertIsNone(r["container_label"])

    def test_not_json_returns_none(self):
        self.assertIsNone(self.row("not json at all"))
        self.assertIsNone(self.row("[1,2]"))

    def test_host_alias_must_be_an_alias(self):
        for bad in ("Host.Example.ts.net", "100.64.0.1", "", "UPPER"):
            with self.assertRaises(ValueError):
                self.row(host_alias=bad)

    def test_inspect_state_sets_oom_and_exit(self):
        r = rs.apply_inspect(self.row(), {"Status": "exited", "Running": False, "OOMKilled": True, "ExitCode": 137})
        assert_valid(self, r)
        self.assertIs(r["oom_killed"], True)
        self.assertEqual(r["exit_code"], 137)

    def test_running_container_has_no_exit_code(self):
        r = rs.apply_inspect(self.row(), {"Status": "running", "Running": True, "OOMKilled": False, "ExitCode": 0})
        self.assertIs(r["oom_killed"], False)
        self.assertIsNone(r["exit_code"])

    def test_bad_inspect_is_ignored(self):
        base = self.row()
        self.assertEqual(rs.apply_inspect(dict(base), None), base)
        self.assertEqual(rs.apply_inspect(dict(base), {"OOMKilled": "yes", "ExitCode": "x"})["oom_killed"], None)


class Rollup(unittest.TestCase):
    def samples(self):
        out = []
        for i, (mem, cpu, pids, rx) in enumerate([("1GiB / 4GiB", "50%", "10", "1MB / 1kB"),
                                                  ("3GiB / 4GiB", "150%", "60", "5MB / 2kB"),
                                                  ("2GiB / 4GiB", "100%", "30", "6MB / 3kB")]):
            out.append(rs.parse_stats_line(stats_line(MemUsage=mem, CPUPerc=cpu, PIDs=pids, NetIO=rx),
                                           ts=f"2026-11-24T20:10:{10 * (i + 1)}Z", host_alias="brrrrrrrr",
                                           attempt_id="att_mock01", project="example-repo", cpu_limit=2))
        out[-1] = rs.apply_inspect(out[-1], {"Running": False, "OOMKilled": False, "ExitCode": 0})
        return out

    def test_rollup_summarises_one_attempt(self):
        r = rs.rollup(self.samples(), attempt_id="att_mock01")
        assert_valid(self, r)
        self.assertEqual((r["scope"], r["attempt_id"], r["project"], r["samples"]), ("attempt_rollup", "att_mock01", "example-repo", 3))
        self.assertEqual(r["ts"], "2026-11-24T20:10:30Z")
        self.assertEqual(r["mem_peak_bytes"], 3 * GiB)
        self.assertEqual(r["mem_limit_bytes"], 4 * GiB)
        self.assertAlmostEqual(r["cpu_ratio"], 1.0)
        self.assertEqual(r["pids"], 60)
        self.assertEqual((r["net_rx_bytes"], r["net_tx_bytes"]), (6 * 10 ** 6, 3000))
        self.assertIs(r["oom_killed"], False)
        self.assertEqual(r["exit_code"], 0)

    def test_rollup_ignores_other_attempts_and_handles_empty(self):
        other = rs.parse_stats_line(stats_line(MemUsage="9GiB / 16GiB"), ts="2026-11-24T20:10:40Z", host_alias="brrrrrrrr",
                                    attempt_id="att_other", project="x", cpu_limit=2)
        r = rs.rollup(self.samples() + [other], attempt_id="att_mock01")
        self.assertEqual(r["mem_peak_bytes"], 3 * GiB)
        self.assertEqual(r["samples"], 3)
        self.assertIsNone(rs.rollup([], attempt_id="att_mock01"))

    def test_any_oom_marks_the_rollup(self):
        s = self.samples()
        s[1] = rs.apply_inspect(s[1], {"Running": True, "OOMKilled": True})
        self.assertIs(rs.rollup(s, attempt_id="att_mock01")["oom_killed"], True)


class Command(unittest.TestCase):
    def test_sampling_command_and_interval(self):
        self.assertEqual(rs.STATS_ARGV, ("docker", "stats", "--no-stream", "--format", "{{json .}}"))
        self.assertEqual(rs.SAMPLE_INTERVAL_S, 10)


if __name__ == "__main__":
    unittest.main()
