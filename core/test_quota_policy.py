#!/usr/bin/env python3
"""The policy is one pure function: snapshot + settings + clock -> decision."""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lokki_core import routing  # noqa: E402
from lokki_core.quota import policy  # noqa: E402

NOW = 1_800_000_000.0
LQ = {"threshold_percent": 30, "rule": "all", "accounts": [], "poll_seconds": 60}


def acct(used, enabled=True, error=None, reset_at=NOW + 3600, window=18000):
    return {"label": "x", "email_masked": "x***@y", "enabled": enabled, "used_percent": used,
            "reset_at": reset_at, "window_seconds": window, "error": error}


def snap(accounts, read_at=NOW - 10):
    return {"read_at": read_at, "accounts": accounts}


class PolicyTests(unittest.TestCase):
    def test_routing_relay_and_direct_ignore_the_quota_but_still_show_it(self):
        d = policy.decide(None, LQ, "relay", NOW, None)
        self.assertEqual((d.relay_on, d.reason, d.per_account), (True, "routing: relay", {}))
        d = policy.decide(snap({"a": acct(99)}), LQ, "direct", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (False, "routing: direct"))
        self.assertEqual(d.per_account, {"a": "low"})

    def test_each_reason_in_the_order_decide_tests_them(self):
        """One row per line of spec 3.4's table but the model's; the guard's row is
        `RoutingTests`'. A snapshot decides whenever there is one: `read_error` only without."""
        low, ok, unknown = acct(90), acct(10), acct(None, error="timeout")
        anyrule = dict(LQ, rule="any")
        # (label, snapshot, low_quota, routing, read_error, relay_on, reason)
        rows = [
            ("relay", None, LQ, "relay", "x", True, "routing: relay"),
            ("direct", snap({"a": low}), LQ, "direct", None, False, "routing: direct"),
            ("no snapshot, a read error", None, LQ, "auto", "HTTP 500", False, "the quota cannot be read"),
            ("no snapshot, no error yet", None, LQ, "auto", None, False, "the quota is not read yet"),
            ("no account enabled", snap({"a": acct(90, enabled=False)}), LQ, "auto", None,
             False, "no account to watch"),
            ("a named account the snapshot lacks", snap({"a": low}), dict(LQ, accounts=["gone"]), "auto", None,
             False, "no account to watch"),
            ("an empty snapshot", snap({}), LQ, "auto", None, False, "no account to watch"),
            ("every watched account unknown", snap({"a": unknown, "b": acct(None)}), LQ, "auto", None,
             False, "no watched account could be read"),
            ("all, every watched account low", snap({"a": low, "b": acct(71)}), LQ, "auto", None,
             True, "every watched account is low"),
            ("all, the read ones low and one unknown", snap({"a": low, "b": unknown}), LQ, "auto", None,
             False, "a watched account could not be read"),
            ("all, one read and not low", snap({"a": low, "b": ok, "c": unknown}), LQ, "auto", None,
             False, "not every watched account is low"),
            ("any, a watched account low", snap({"a": low, "b": unknown}), anyrule, "auto", None,
             True, "a watched account is low"),
            ("any, none read low and one unknown", snap({"a": ok, "b": unknown}), anyrule, "auto", None,
             False, "a watched account could not be read"),
            ("any, every watched account read and not low", snap({"a": ok, "b": acct(20)}), anyrule, "auto", None,
             False, "no watched account is low"),
            ("a snapshot with a read error decides from the snapshot", snap({"a": low}), LQ, "auto", "HTTP 500",
             True, "every watched account is low"),
        ]
        returned = set()
        for label, s, lq, routing, error, relay_on, reason in rows:
            with self.subTest(label):
                d = policy.decide(s, lq, routing, NOW, error)
                self.assertEqual((d.relay_on, d.reason), (relay_on, reason))
                returned.add(d.reason)
        # `for_model` passes a cutting decision through with its own reason.
        cut = policy.decide(snap({"a": low}), LQ, "auto", NOW, None)
        returned.add(policy.for_model(cut, dict(LQ, models=["m1"]), "m2").reason)
        # The rows reach every reason but the guard's, which `RoutingTests` binds: no entry of
        # `REASONS` is one nothing returns, and nothing returned is missing from it.
        self.assertEqual(returned | {policy.FAILED}, set(policy.REASONS))

    def test_the_api_doc_lists_every_reason(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "app", "api.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        listed = text.split("`decision.reason` is one of these", 1)[1].split("\n\n", 2)[1]
        self.assertEqual([line.split("`")[1] for line in listed.splitlines()], list(policy.REASONS))
        self.assertEqual(len(set(policy.REASONS)), len(policy.REASONS))

    def test_all_rule_needs_every_account_low(self):
        d = policy.decide(snap({"a": acct(80), "b": acct(50)}), LQ, "auto", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (False, "not every watched account is low"))
        self.assertEqual(d.per_account, {"a": "low", "b": "ok"})
        d = policy.decide(snap({"a": acct(80), "b": acct(71)}), LQ, "auto", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (True, "every watched account is low"))

    def test_any_rule_needs_one(self):
        lq = dict(LQ, rule="any")
        d = policy.decide(snap({"a": acct(80), "b": acct(50)}), lq, "auto", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (True, "a watched account is low"))
        d = policy.decide(snap({"a": acct(10), "b": acct(50)}), lq, "auto", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (False, "no watched account is low"))

    def test_the_threshold_is_a_strict_remaining_bound(self):
        lq = dict(LQ, threshold_percent=30)
        self.assertEqual(policy.decide(snap({"a": acct(70)}), lq, "auto", NOW, None).per_account["a"], "ok")    # remaining 30
        self.assertEqual(policy.decide(snap({"a": acct(71)}), lq, "auto", NOW, None).per_account["a"], "low")   # remaining 29

    def test_zero_used_is_ok(self):
        self.assertEqual(policy.decide(snap({"a": acct(0)}), LQ, "auto", NOW, None).per_account["a"], "ok")

    def test_unknown_by_error_missing_percent_age_or_window(self):
        for a in (acct(None, error="HTTP 500"), acct(None), acct(90, window=604800)):
            d = policy.decide(snap({"a": a}), LQ, "auto", NOW, None)
            self.assertEqual(d.per_account["a"], "unknown", a)
            self.assertEqual((d.relay_on, d.reason), (False, "no watched account could be read"))

    def test_staleness_boundary(self):
        for age, state in ((60 * 2.9, "low"), (60 * 3.1, "unknown")):
            d = policy.decide(snap({"a": acct(90)}, read_at=NOW - age), LQ, "auto", NOW, None)
            self.assertEqual(d.per_account["a"], state, age)

    def test_a_reset_at_of_zero_is_absent_not_a_reset(self):
        for reset in (0, -5, None):
            d = policy.decide(snap({"a": acct(95, reset_at=reset)}), LQ, "auto", NOW, None)
            self.assertEqual(d.per_account["a"], "low", reset)

    def test_a_window_that_reset_reads_as_full(self):
        d = policy.decide(snap({"a": acct(90, reset_at=NOW - 1)}), LQ, "auto", NOW, None)
        self.assertEqual(d.per_account["a"], "ok")

    def test_unknown_counts_as_not_low_under_both_rules(self):
        both = snap({"a": acct(90), "b": acct(None, error="timeout")})
        self.assertFalse(policy.decide(both, LQ, "auto", NOW, None).relay_on)                    # all: b is not low
        self.assertTrue(policy.decide(both, dict(LQ, rule="any"), "auto", NOW, None).relay_on)   # any: a is low

    def test_scope_names_accounts_and_an_empty_scope_is_direct(self):
        s = snap({"a": acct(90), "b": acct(10, enabled=False)})
        d = policy.decide(s, dict(LQ, accounts=["b"]), "auto", NOW, None)
        self.assertEqual(d.watched, frozenset({"b"}))
        self.assertFalse(d.relay_on)
        d = policy.decide(snap({}), LQ, "auto", NOW, None)
        self.assertEqual((d.relay_on, d.reason), (False, "no account to watch"))
        d = policy.decide(s, LQ, "auto", NOW, None)              # empty setting = every enabled account
        self.assertEqual(d.watched, frozenset({"a"}))

    def test_disabled_accounts_are_outside_the_default_scope_and_a_named_one_is_inside(self):
        s = snap({"a": acct(90, enabled=False)})
        self.assertEqual((policy.decide(s, LQ, "auto", NOW, None).reason), "no account to watch")
        self.assertEqual(policy.watched(s, LQ), frozenset())
        self.assertEqual(policy.watched(s, dict(LQ, accounts=["a", "gone"])), frozenset({"a"}))
        self.assertTrue(policy.decide(s, dict(LQ, accounts=["a"]), "auto", NOW, None).relay_on)

    def test_every_account_has_its_own_state_watched_or_not(self):
        s = snap({"a1": acct(90), "b2": acct(10), "d4": acct(None, enabled=False, error="disabled")})
        lq = dict(LQ, accounts=["a1"])
        self.assertEqual(policy.states(s, lq, NOW), {"a1": "low", "b2": "ok", "d4": "unknown"})
        self.assertEqual(policy.watched(s, lq), frozenset({"a1"}))
        d = policy.decide(s, lq, "auto", NOW, None)
        self.assertEqual(d.per_account, {"a1": "low", "b2": "ok", "d4": "unknown"})
        self.assertEqual((d.relay_on, d.reason), (True, "every watched account is low"))   # b2 is not watched
        self.assertEqual(policy.states(None, lq, NOW), {})

    def test_for_model_passes_an_unnamed_model_only_when_the_quota_cuts(self):
        cut = policy.Decision(True, policy.ALL_LOW, {"a": "low"}, frozenset({"a"}))
        passed = policy.Decision(False, policy.NOT_ALL_LOW, {"a": "ok"}, frozenset({"a"}))
        through = policy.Decision(False, "this model is not cut when low", {"a": "low"}, frozenset({"a"}))
        # (label, decision, models, model, the decision it answers)
        rows = [
            ("a named model is cut", cut, ["m1"], "m1", cut),
            ("one of several named", cut, ["m0", "m1"], "m1", cut),
            ("an unnamed model passes", cut, ["m1"], "m2", through),
            ("no model is not named", cut, ["m1"], None, through),
            ("a model that is not a string is not named", cut, ["m1"], 7, through),
            ("a list is not named", cut, ["m1"], ["m1"], through),
            ("a suffixed model counts as its model", cut, ["gpt-x"], "gpt-x(high)", cut),
            ("an entry that names the suffix matches the same", cut, ["gpt-x(high)"], "gpt-x(high)", cut),
            ("an entry that names a suffix is not the plain model", cut, ["gpt-x(high)"], "gpt-x", through),
            ("another model at a level is not named", cut, ["gpt-x"], "gpt-y(high)", through),
            ("an unclosed suffix is no suffix", cut, ["gpt-x"], "gpt-x(high", through),
            ("one suffix is removed, the last", cut, ["gpt-x(a)"], "gpt-x(a)(b)", cut),
            ("a longer name is not the entry", cut, ["gpt-x"], "gpt-xy", through),
            ("case counts", cut, ["gpt-x"], "GPT-X", through),
            ("no models cuts every model", cut, [], "m2", cut),
            ("a passing decision is unchanged", passed, ["m1"], "m2", passed),
            ("a passing decision is unchanged for a named model", passed, ["m1"], "m1", passed),
            ("a passing decision is unchanged without a model", passed, ["m1"], None, passed),
        ]
        for label, decision, models, model, expected in rows:
            with self.subTest(label):
                self.assertEqual(policy.for_model(decision, dict(LQ, models=models), model), expected)
        with self.subTest("a settings dict with no models key"):
            self.assertEqual(policy.for_model(cut, LQ, "m2"), cut)
        self.assertEqual(policy.REASONS[-2:], ("this model is not cut when low", policy.FAILED))
        self.assertEqual(policy.UNNAMED, "this model is not cut when low")

    def test_fixed_answers_relay_and_direct_and_nothing_for_auto(self):
        states, watched = {"a": "low"}, frozenset({"a"})
        self.assertEqual(policy.fixed("relay", states, watched), policy.Decision(True, "routing: relay", states, watched))
        self.assertEqual(policy.fixed("direct", states, watched), policy.Decision(False, "routing: direct", states, watched))
        self.assertIsNone(policy.fixed("auto", states, watched))


class RoutingTests(unittest.TestCase):
    """`routing.decide_now` is the policy's one caller."""
    def setUp(self):
        from lokki_core import config
        saved = (config.LOW_QUOTA, config.ROUTING, routing._last, routing._raised, list(routing.EVENTS))
        self.addCleanup(self._restore, config, saved)
        config.LOW_QUOTA = dict(LQ)
        config.ROUTING = "auto"
        routing.reset()
        self.err = io.StringIO()
        patcher = mock.patch.object(sys, "stderr", self.err)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = config

    @staticmethod
    def _restore(config, saved):
        config.LOW_QUOTA, config.ROUTING, routing._last, routing._raised, events = saved
        routing.EVENTS.clear()
        routing.EVENTS.extend(events)

    def now(self):
        import time
        return time.time()

    def test_a_raising_rule_passes_requests_through_and_says_its_type_once(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        with mock.patch.object(policy, "decide", side_effect=ValueError("secret-text")):
            first = routing.decide_now(s)
            second = routing.decide_now(s)
        self.assertEqual((first.relay_on, first.reason), (False, policy.FAILED))
        self.assertEqual(policy.FAILED, "the quota rule failed")
        self.assertEqual(second.reason, policy.FAILED)
        out = self.err.getvalue()
        self.assertNotIn("secret-text", out)
        self.assertEqual(out.count("[routing] the quota rule raised ValueError"), 1)
        self.assertEqual(out.count("[routing] relay off: the quota rule failed"), 1)
        with mock.patch.object(policy, "decide", side_effect=KeyError("secret-text")):
            routing.decide_now(s)
        self.assertEqual(self.err.getvalue().count("[routing] the quota rule raised KeyError"), 1)
        with mock.patch.object(policy, "decide", side_effect=ValueError("secret-text")):
            routing.decide_now(s)
        self.assertEqual(self.err.getvalue().count("[routing] the quota rule raised ValueError"), 2)   # a change of type

    def test_a_call_that_does_not_raise_forgets_the_last_type(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        with mock.patch.object(policy, "decide", side_effect=ValueError("x")):
            routing.decide_now(s)
        routing.decide_now(s)
        with mock.patch.object(policy, "decide", side_effect=ValueError("x")):
            routing.decide_now(s)
        self.assertEqual(self.err.getvalue().count("the quota rule raised ValueError"), 2)

    def test_relay_and_direct_never_call_the_rule(self):
        s = snap({"a": acct(90), "b": acct(10)}, read_at=self.now())
        self.config.LOW_QUOTA = dict(LQ, accounts=["a"])
        with mock.patch.object(policy, "decide", side_effect=AssertionError("called")) as m:
            self.config.ROUTING = "relay"
            d = routing.decide_now(s)
            self.assertEqual((d.relay_on, d.reason), (True, "routing: relay"))
            self.assertEqual(d.per_account, {"a": "low", "b": "ok"})
            self.assertEqual(d.watched, frozenset({"a"}))
            self.config.ROUTING = "direct"
            d = routing.decide_now(s)
            self.assertEqual((d.relay_on, d.reason), (False, "routing: direct"))
            self.assertEqual(d.per_account, {"a": "low", "b": "ok"})
            m.assert_not_called()
        self.assertNotIn("raised", self.err.getvalue())

    def test_a_raising_rule_keeps_the_states_and_the_scope_it_was_given(self):
        s = snap({"a": acct(90), "b": acct(10)}, read_at=self.now())
        self.config.LOW_QUOTA = dict(LQ, accounts=["a"])
        with mock.patch.object(policy, "decide", side_effect=ValueError("x")):
            d = routing.decide_now(s)
        self.assertEqual((d.relay_on, d.reason), (False, policy.FAILED))
        self.assertEqual(d.per_account, {"a": "low", "b": "ok"})
        self.assertEqual(d.watched, frozenset({"a"}))

    def test_a_raise_while_stating_or_scoping_under_auto_keeps_neither(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        for name in ("states", "watched"):
            with self.subTest(name), mock.patch.object(policy, name, side_effect=TypeError("x")):
                d = routing.decide_now(s)
            self.assertEqual((d.relay_on, d.reason, d.per_account, d.watched), (False, policy.FAILED, {}, frozenset()))

    def test_two_models_change_the_route_without_a_line_or_an_event(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        self.config.LOW_QUOTA = dict(LQ, models=["m1"])
        routes = [routing.decide_now(s, model=m) for m in ("m1", "m2", "m1")]
        self.assertEqual([(d.relay_on, d.reason) for d in routes],
                         [(True, policy.ALL_LOW), (False, policy.UNNAMED), (True, policy.ALL_LOW)])
        self.assertEqual(self.err.getvalue().count("[routing]"), 1)
        self.assertEqual(self.err.getvalue().count("[routing] relay on: every watched account is low"), 1)
        self.assertEqual([(e["relay_on"], e["reason"]) for e in routing.events()], [(True, policy.ALL_LOW)])
        self.assertEqual((routing.last().relay_on, routing.last().reason), (True, policy.ALL_LOW))

    def test_models_are_autos_rule_so_relay_and_direct_ignore_them(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        self.config.LOW_QUOTA = dict(LQ, models=["m1"])
        for mode, relay_on, reason in (("relay", True, policy.RELAY), ("direct", False, policy.DIRECT)):
            self.config.ROUTING = mode
            for model in ("m1", "m2", None):
                with self.subTest(mode, model=model):
                    d = routing.decide_now(s, model=model)
                    self.assertEqual((d.relay_on, d.reason), (relay_on, reason))

    def test_a_request_for_an_unnamed_model_first_in_a_row_logs_the_quota_decision(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        self.config.LOW_QUOTA = dict(LQ, models=["m1"])
        d = routing.decide_now(s, model="m2")
        self.assertEqual(d.reason, policy.UNNAMED)
        self.assertIn("[routing] relay on: every watched account is low", self.err.getvalue())
        self.assertNotIn(policy.UNNAMED, self.err.getvalue())
        self.assertEqual([e["reason"] for e in routing.events()], [policy.ALL_LOW])

    def test_auto_states_the_accounts_once_per_decision(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        with mock.patch.object(policy, "states", wraps=policy.states) as states:
            routing.decide_now(s)
        self.assertEqual(states.call_count, 1)

    def test_a_raise_while_stating_the_accounts_under_relay_leaves_relay_with_no_states(self):
        s = snap({"a": acct(90)}, read_at=self.now())
        self.config.ROUTING = "relay"
        with mock.patch.object(policy, "states", side_effect=TypeError("secret-text")):
            d = routing.decide_now(s)
        self.assertEqual((d.relay_on, d.reason, d.per_account), (True, "routing: relay", {}))
        self.assertNotIn("secret-text", self.err.getvalue())
        self.assertIn("the quota rule raised TypeError", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
