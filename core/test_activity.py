#!/usr/bin/env python3
"""`GET /api/activity`'s body: what the run records hold over a window, read from
the end of the run log under its lock and counted after it, and the list of
decision changes `routing.decide_now` keeps."""
import collections
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import activity, config, core, routing  # noqa: E402
from lokki_core.quota import policy  # noqa: E402

NOW = 1_790_000_000
DAY = 86400
WRITER_FIELDS = {
    "relay": {"mode": "stealth", "model": "gpt-x", "client": "default", "cut": "marker",
              "cuts": ["marker"], "real_completed": False, "verdict": "usage-discarded",
              "truncated": False, "segs": 1, "delivered_chars": 120, "elapsed_s": 2.5,
              "error": None, "upstream_error": None, "upstream_status": 200, "usage": None,
              "client_gone": False},
    "passthrough": {"reason": "low quota mode off", "model": "gpt-x", "client": "default",
                    "stream": True, "status": 200, "cut": "terminal", "error": None,
                    "usage": None, "elapsed_s": 1.5},
    "images": {"model": "gpt-image", "client": "default", "status": 200, "images_out": 1,
               "error": None, "upstream_error": None, "elapsed_s": 9.0},
}


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def rec(ts_offset, route="relay", **fields):
    """One record as the writers and `log_run` write it, `ts_offset` seconds from NOW.
    `route=None` is an older relay record, written before records carried a route."""
    out = {} if route is None else {"route": route}
    out.update(WRITER_FIELDS[route or "relay"])
    out.update(ts=iso(NOW + ts_offset), revision="abc1234", config_fingerprint={"pacing": 0},
               seq=1, prev_hash="0" * 64, hash="f" * 64)
    out.update(fields)
    return out


def write(path, records, raw_lines=()):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for line in raw_lines:
            f.write(line)


class ActivityTests(unittest.TestCase):
    def setUp(self):
        activity.clear_cache()
        self.addCleanup(activity.clear_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = os.path.join(tmp.name, "runs.jsonl")
        patch = mock.patch.object(core, "RUNS_LOG", self.log)
        patch.start()
        self.addCleanup(patch.stop)

    def answer(self, records, window="24h", raw_lines=()):
        write(self.log, records, raw_lines)
        activity.clear_cache()
        return activity.answer(window, now=NOW)

    def test_a_record_falls_in_the_bucket_its_ts_falls_in(self):
        for window, count in (("24h", 24), ("7d", 28)):
            span, step = activity.WINDOWS[window]
            start = NOW - span
            for ts, bucket in ((start, 0), (start + step - 1, 0), (start + step, 1),
                               (NOW, count - 1), (start - 1, None), (NOW + 1, None)):
                body = self.answer([rec(ts - NOW)], window)
                self.assertEqual((body["window"], body["now"], body["from"]), (window, NOW, start))
                self.assertEqual(len(body["buckets"]), count)
                self.assertEqual([b["start"] for b in body["buckets"]],
                                 [start + k * step for k in range(count)])
                hit = [k for k, b in enumerate(body["buckets"]) if b["relay"]]
                self.assertEqual(hit, [] if bucket is None else [bucket], (window, ts))
                self.assertEqual(body["coverage"]["records"], 0 if bucket is None else 1)

    def test_each_relay_record_counts_in_one_class_in_precedence_order(self):
        rows = (
            (dict(verdict="billed", error="boom", cut="error"), "finalized", None),
            (dict(verdict=None, real_completed=True, cut="terminal"), "finalized", None),
            (dict(cut="error"), "error", None),
            (dict(cut="client_gone", error="BrokenPipeError"), "error", None),
            (dict(cut="client_gone"), "client_gone", None),
            (dict(cut="marker"), "cut", "marker"),
            (dict(cut="budget"), "cut", "budget"),
            (dict(cut=None), "cut", "unknown"),
            (dict(route=None), "cut", "marker"),        # an older record, without a route
        )
        records = []
        for i, (fields, _, _) in enumerate(rows):
            r = rec(-60 * (len(rows) - i), **fields)
            records.append(r)
        no_cut = rec(-30)
        del no_cut["cut"]
        records.append(no_cut)
        expected = [cls for _, cls, _ in rows] + ["cut"]
        self.assertEqual([activity.outcome(r) for r in records], expected)
        body = self.answer(records)
        classes = collections.Counter(expected)
        self.assertEqual({k: body["relay"][k] for k in ("finalized", "cut", "client_gone", "error")},
                         {k: classes[k] for k in ("finalized", "cut", "client_gone", "error")})
        self.assertEqual(body["relay"]["by_cut"], {"marker": 2, "budget": 1, "unknown": 2})
        self.assertEqual(body["totals"]["relay"], len(records))
        self.assertEqual(sum(body["relay"][k] for k in ("finalized", "cut", "client_gone", "error")),
                         body["totals"]["relay"])

    def test_an_error_counts_inside_its_route(self):
        rows = (
            (rec(-90, cut="error", error="response.failed"), True),
            (rec(-80, "passthrough", cut="error", status=502), True),
            (rec(-70, "passthrough", error="URLError", cut="eof"), True),
            (rec(-60, "passthrough"), False),
            (rec(-50, "images", error="timeout", status=None), True),
            (rec(-40, "images", status=429, upstream_error={"status": 429, "code": None, "type": "rate"}), True),
            (rec(-30, "images", status=400), True),
            (rec(-20, "images", status=399), False),
            (rec(-10, "images", status=200), False),
        )
        records = [r for r, _ in rows]
        self.assertEqual([activity.outcome(r) == "error" for r in records], [e for _, e in rows])
        body = self.answer(records)
        totals = body["totals"]
        self.assertEqual(totals["errors"], sum(e for _, e in rows))
        self.assertEqual((totals["relay"], totals["passthrough"], totals["images"]), (1, 3, 5))
        self.assertEqual(totals["requests"], totals["relay"] + totals["passthrough"] + totals["images"])
        self.assertEqual(body["relay"]["error"], 1)
        self.assertEqual(sum(b["errors"] for b in body["buckets"]), totals["errors"])

    def test_pass_through_counts_by_reason_and_by_cut_with_unknown(self):
        records = [rec(-60, "passthrough", reason="low quota mode off", cut="terminal"),
                   rec(-50, "passthrough", reason="low quota mode off", cut="eof"),
                   rec(-40, "passthrough", reason="routing: direct", cut="client_gone"),
                   rec(-30, "passthrough", reason="routing: direct", cut="error", status=502),
                   rec(-20, "passthrough", reason="no account in scope is low", cut=None),
                   rec(-10)]
        body = self.answer(records)
        self.assertEqual(body["passthrough"], {
            "by_reason": {"low quota mode off": 2, "routing: direct": 2, "no account in scope is low": 1},
            "by_cut": {"terminal": 1, "eof": 1, "client_gone": 1, "error": 1, "unknown": 1}})

    def test_each_recent_row_carries_the_outcome_its_record_counted_under(self):
        shapes = (dict(verdict="billed"), dict(cut="error"), dict(cut="client_gone"), dict(cut="marker"),
                  dict(route="passthrough"), dict(route="passthrough", cut="error"),
                  dict(route="passthrough", cut="client_gone"), dict(route="images"),
                  dict(route="images", status=500))
        records = [rec(-600 + i, **shapes[i % len(shapes)]) for i in range(3 * len(shapes))]
        body = self.answer(records)
        recent = body["recent"]
        self.assertEqual(len(recent), len(records))
        relay = collections.Counter(r["outcome"] for r in recent if r["route"] == "relay")
        self.assertEqual(dict(relay), {k: body["relay"][k] for k in ("finalized", "cut", "client_gone", "error")})
        self.assertEqual(sum(r["outcome"] == "error" for r in recent), body["totals"]["errors"])
        self.assertEqual([r["outcome"] for r in recent], [activity.outcome(r) for r in reversed(records)])

    def test_tokens_sum_the_usage_whitelist_in_both_cached_shapes(self):
        records = [
            rec(-50, usage={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                            "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 0}}),
            rec(-40, "passthrough", usage={"input_tokens": 200, "output_tokens": 20, "cached_tokens": 7}),
            rec(-30, usage={"input_tokens": 1, "output_tokens": 30,
                            "output_tokens_details": {"reasoning_tokens": 12}}),
            rec(-20, "passthrough", usage={"input_tokens": 2, "output_tokens": 3, "reasoning_tokens": 5}),
            rec(-10, cut="marker", usage=None),
        ]
        self.assertEqual(self.answer(records)["tokens"],
                         {"runs": 4, "input": 303, "output": 63, "cached": 47, "reasoning": 17})

    def test_latency_is_the_nearest_rank_p50_and_p90_per_route_and_overall(self):
        for values, p50, p90 in ((list(range(1, 11)), 5, 9), ([1, 2, 3, 4], 2, 4),
                                 ([7.5], 7.5, 7.5), ([], None, None)):
            self.assertEqual((activity.percentile(values, 50), activity.percentile(values, 90)),
                             (p50, p90), values)
        records = [rec(-100 + v, elapsed_s=v) for v in range(1, 11)]
        records.append(rec(-1, "images", elapsed_s=30))
        records.append(rec(0, "passthrough", elapsed_s=None))
        latency = self.answer(records)["latency"]
        self.assertEqual(latency["relay"], {"p50": 5, "p90": 9})
        self.assertEqual(latency["images"], {"p50": 30, "p90": 30})
        self.assertEqual(latency["passthrough"], {"p50": None, "p90": None})
        self.assertEqual(latency["all"], {"p50": 6, "p90": 10})

    def test_models_and_clients_fold_past_the_eighth_into_other(self):
        for name, key in (("model", "models"), ("client", "clients")):
            counts = {"m-j": 1, "m-i": 2, "m-h": 3, "m-g": 3, "m-f": 4, "m-e": 5, "m-d": 6,
                      "m-c": 7, "m-b": 8, "m-a": 9}
            records = [rec(-1000 + i, **{name: n}) for i, (n, c) in enumerate(
                (n, c) for n, c in counts.items() for _ in range(c))]
            self.assertEqual(self.answer(records)[key],
                             [{name: n, "runs": c} for n, c in (("m-a", 9), ("m-b", 8), ("m-c", 7), ("m-d", 6),
                                                               ("m-e", 5), ("m-f", 4), ("m-g", 3), ("m-h", 3))]
                             + [{name: "other", "runs": 3}])
            eight = [rec(-100 + i, **{name: f"m-{i}"}) for i in range(7)] + [rec(-1, **{name: None})]
            rows = self.answer(eight)[key]
            self.assertEqual(len(rows), 8)
            self.assertNotIn("other", [r[name] for r in rows])
            self.assertIn({name: "unknown", "runs": 1}, rows)

    def test_coverage_counts_torn_undated_and_cut_lines_apart(self):
        body = self.answer([rec(-30), rec(-20, ts="yesterday"), dict(rec(-10), route="moderation")],
                           raw_lines=('[1, 2]\n', "[" * 100000 + "]" * 100000 + "\n", '{"ts": "2026-'))
        self.assertEqual(body["coverage"], {"oldest": NOW - 30, "records": 2, "undated": 1,
                                            "skipped": 3, "complete": True})
        self.assertEqual(body["totals"]["requests"], 1)
        self.assertEqual(sum(b["relay"] for b in body["buckets"]), 1)
        records = [rec(-100 + i, delivered_chars=1000 + i) for i in range(4)]
        line = len(json.dumps(records[0], ensure_ascii=False)) + 1
        with mock.patch.object(config, "RUNS_MAX_BYTES", 2 * line + line // 2):
            body = self.answer(records)
        self.assertEqual(body["coverage"], {"oldest": NOW - 98, "records": 2, "undated": 0,
                                            "skipped": 0, "complete": False})

    def test_a_window_across_a_rotation_seam_reads_each_record_once(self):
        write(self.log + ".2", [rec(-3 * DAY), rec(-2 * DAY)])
        # `.1` holds two records older than the window, then one at exactly `from`.
        write(self.log + ".1", [rec(-DAY - 120), rec(-DAY - 60)] + [rec(-DAY + 60 * i, seq=i) for i in range(5)])
        live = [rec(-60 * i) for i in range(4, 0, -1)]
        line = len(json.dumps(live[0], ensure_ascii=False)) + 1
        # `oldest` shows where the copy stopped. A chunk that holds `.1` whole reads it from
        # byte 0; chunks narrower than a line stop at the first whole line older than `from`,
        # whose `ts` lies past the chunk its line starts in, and drop the cut line before it.
        # Either way `.2` is never read.
        for chunk, oldest in ((activity.CHUNK, NOW - DAY - 120), (line // 4, NOW - DAY - 60)):
            with mock.patch.object(activity, "CHUNK", chunk):
                body = self.answer(live)
                rotated_only = self.answer([])
            self.assertEqual((body["coverage"]["records"], body["totals"]["relay"]), (9, 9), chunk)
            self.assertEqual(body["coverage"]["oldest"], oldest, chunk)
            self.assertTrue(body["coverage"]["complete"], chunk)
            self.assertEqual(rotated_only["coverage"]["records"], 5, chunk)

    def test_the_byte_cap_marks_the_window_incomplete(self):
        records = [rec(-DAY - 100)] + [rec(-4000 + 60 * i, delivered_chars=1000 + i) for i in range(40)]
        line = len(json.dumps(records[1], ensure_ascii=False)) + 1
        with mock.patch.object(config, "RUNS_MAX_BYTES", 10 * line + line // 2):
            capped = self.answer(records)
        self.assertEqual(capped["coverage"]["complete"], False)
        self.assertEqual(capped["coverage"]["records"], 10)
        self.assertEqual(capped["coverage"]["oldest"], NOW - 4000 + 60 * 30)
        whole = self.answer(records)
        self.assertEqual((whole["coverage"]["complete"], whole["coverage"]["records"],
                          whole["coverage"]["oldest"]), (True, 40, NOW - DAY - 100))
        # Lines the byte search cannot read: the cap still ends the copy, and an old record
        # the parse finds inside it still makes the window complete.
        older = [rec(-2 * DAY)] + records
        with open(self.log, "w", encoding="utf-8") as f:
            for r in older:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        compact = len(json.dumps(records[1], separators=(",", ":"))) + 1
        first = len(json.dumps(older[0], separators=(",", ":"))) + 1
        for cap, complete, counted in ((10 * compact + compact // 2, False, 10),
                                       (os.path.getsize(self.log) - first // 2, True, 40)):
            with mock.patch.object(config, "RUNS_MAX_BYTES", cap), mock.patch.object(activity, "CHUNK", compact):
                activity.clear_cache()
                coverage = activity.answer("24h", now=NOW)["coverage"]
            self.assertEqual((coverage["complete"], coverage["records"]), (complete, counted), cap)

    def test_an_empty_log_is_complete_with_no_oldest(self):
        body = activity.answer("24h", now=NOW)
        self.assertEqual(body["coverage"], {"oldest": None, "records": 0, "undated": 0,
                                            "skipped": 0, "complete": True})
        self.assertEqual([(b["relay"], b["passthrough"], b["images"], b["errors"]) for b in body["buckets"]],
                         [(0, 0, 0, 0)] * 24)
        self.assertEqual(body["latency"], {k: {"p50": None, "p90": None}
                                           for k in ("relay", "passthrough", "images", "all")})
        self.assertEqual((body["recent"], body["models"], body["clients"]), ([], [], []))

    def test_recent_rows_are_allow_listed_bounded_and_newest_first(self):
        full = rec(-500, error="x" * 500, upstream_error={"status": 429, "code": "rate_limit", "type": "t"},
                   upstream_status=200, usage={"input_tokens": 11, "output_tokens": 22},
                   resp_id="resp_1", params_forwarded=["temperature"], headers_forwarded=["openai-beta"],
                   knobs={"seg_budget": 1}, pad_tools=True, prompt_chars=40, deltas=3, events=4)
        row = self.answer([full])["recent"][0]
        self.assertEqual(row, {"ts": NOW - 500, "route": "relay", "model": "gpt-x", "client": "default",
                               "mode": "stealth", "cut": "marker", "verdict": "usage-discarded",
                               "outcome": "error", "status": 429, "elapsed_s": 2.5, "delivered_chars": 120,
                               "segs": 1, "input_tokens": 11, "output_tokens": 22, "reason": None,
                               "error": "x" * 200})
        self.assertEqual(self.answer([rec(-1, upstream_status=503)])["recent"][0]["status"], 503)
        self.assertEqual(self.answer([rec(-1, "passthrough", status=404, cut="error")])["recent"][0]["status"], 404)
        many = [rec(-600 + i, segs=i) for i in range(60)]
        recent = self.answer(many)["recent"]
        self.assertEqual([r["segs"] for r in recent], list(range(59, 9, -1)))

    def test_decision_changes_are_kept_newest_first_up_to_100(self):
        self.addCleanup(setattr, routing, "_last", routing._last)
        self.addCleanup(routing.EVENTS.extend, list(routing.EVENTS))
        self.addCleanup(routing.EVENTS.clear)
        routing.reset()
        self.assertEqual(routing.events(), [])
        decisions = [policy.Decision(i % 2 == 0, f"reason {i}") for i in range(105)]
        with mock.patch.object(policy, "decide", side_effect=decisions + [decisions[-1]]), \
                mock.patch.object(config, "ROUTING", "auto"), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            before = int(time.time())
            for _ in range(106):
                routing.decide_now(snapshot=None)
        self.assertEqual(len(err.getvalue().splitlines()), 105)
        events = routing.events()
        self.assertEqual(len(events), 100)
        self.assertEqual([e["reason"] for e in events], [f"reason {i}" for i in range(104, 4, -1)])
        self.assertEqual(events[0], {"at": events[0]["at"], "relay_on": True, "reason": "reason 104"})
        self.assertTrue(all(isinstance(e["at"], int) and e["at"] >= before for e in events))
        events[0]["reason"] = "changed by a caller"
        self.assertEqual(routing.events()[0]["reason"], "reason 104")
        routing.reset()
        self.assertEqual(routing.events(), [])

    def test_the_cache_lives_ten_seconds_ends_at_a_write_and_never_shares_a_window(self):
        clock = [1000.0]
        patch = mock.patch.object(activity, "_clock", lambda: clock[0])
        patch.start()
        self.addCleanup(patch.stop)
        write(self.log, [rec(-60)])
        first = activity.answer("24h", now=NOW)
        clock[0] += 9
        self.assertIs(activity.answer("24h", now=NOW)["buckets"], first["buckets"])
        clock[0] += 2
        self.assertIsNot(activity.answer("24h", now=NOW)["buckets"], first["buckets"])
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec(-30)) + "\n")
        clock[0] += 1
        self.assertEqual(activity.answer("24h", now=NOW)["coverage"]["records"], 2)
        week = activity.answer("7d", now=NOW)
        self.assertEqual((week["window"], len(week["buckets"])), ("7d", 28))
        self.addCleanup(setattr, routing, "_last", routing._last)
        self.addCleanup(routing.EVENTS.extend, list(routing.EVENTS))
        self.addCleanup(routing.EVENTS.clear)
        routing.reset()
        with mock.patch.object(policy, "decide", return_value=policy.Decision(False, "routing: direct")), \
                mock.patch.object(config, "ROUTING", "auto"), \
                mock.patch("sys.stderr", io.StringIO()):
            routing.decide_now(snapshot=None)
        clock[0] += 1
        self.assertEqual([e["reason"] for e in activity.answer("7d", now=NOW)["events"]], ["routing: direct"])

    def test_only_the_ts_search_runs_under_the_run_log_lock(self):
        chunk = 4096
        write(self.log + ".1", [rec(-7200 + i) for i in range(60)])
        write(self.log, [rec(-60 + i) for i in range(50)])
        chunks = sum(-(-os.path.getsize(p) // chunk) for p in (self.log, self.log + ".1"))
        self.assertGreater(chunks, 2)
        loads_saw, epoch_saw = [], []
        real_loads, real_epoch = json.loads, activity._epoch

        def loads(*args, **kwargs):
            loads_saw.append(core._runs_lock.locked())
            return real_loads(*args, **kwargs)

        def epoch(text):
            epoch_saw.append(core._runs_lock.locked())
            return real_epoch(text)

        with mock.patch.object(activity, "CHUNK", chunk), mock.patch.object(activity.json, "loads", loads), \
                mock.patch.object(activity, "_epoch", epoch):
            body = activity.answer("24h", now=NOW)
        self.assertEqual(body["coverage"]["records"], 110)
        self.assertEqual(set(loads_saw), {False})
        self.assertGreaterEqual(len(loads_saw), 110)
        self.assertTrue(1 <= epoch_saw.count(True) <= chunks, (epoch_saw.count(True), chunks))


if __name__ == "__main__":
    unittest.main()
