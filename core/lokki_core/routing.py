"""The decision, computed per request, and the one stderr line when it changes.
Unknown counts as not low: a dead management API makes every request a
full-price one while the endpoint looks healthy, so every change of the
decision or its reason is said once, and /healthz carries the decision. Each
change is also kept, the last hundred in memory, for `/api/activity`."""
import collections
import sys
import threading
import time

from . import config
from .quota import policy, poller

_lock = threading.Lock()
_last = None
_raised = None                             # the type name of the last raise the guard caught
EVENTS = collections.deque(maxlen=100)     # {at, relay_on, reason}, oldest first


def reset():
    global _last, _raised
    with _lock:
        _last = None
        _raised = None
        EVENTS.clear()


def last():
    return _last


def events():
    """The decision changes since the core started, newest first, as copies."""
    with _lock:
        return [dict(e) for e in reversed(EVENTS)]


def decide_now(snapshot=..., model=...):
    """The decision at this moment. A caller that also shows the snapshot passes the
    one it read, so the rows and the states come from the same reading. `model` is a
    request's, as its body gave it (None for none); left out, there is no request and
    the answer is the quota's. The line and `EVENTS` follow the quota's decision only,
    so two models in a row change a route and neither. Only under Auto."""
    global _last, _raised
    if snapshot is ...:
        snapshot = poller.POLLER.latest()
    now = time.time()
    mode = config.ROUTING
    raised = None
    per_account, watch = {}, frozenset()
    try:
        per_account, watch = (policy.states(snapshot, config.LOW_QUOTA, now),
                              policy.watched(snapshot, config.LOW_QUOTA))
        # Relay and Direct never reach the rule.
        d = policy.fixed(mode, per_account, watch) \
            or policy.decide(snapshot, config.LOW_QUOTA, mode, now, poller.POLLER.status()["last_error"],
                             per_account, watch)
    except Exception as e:
        # The type only: an exception's text is not the log's to keep. A raise here must
        # not become a 500 on every request, so the request goes through at full price. What
        # was stated before the raise stays, so Accounts still shows each account's own state.
        raised = type(e).__name__
        d = policy.fixed(mode, per_account, watch) or policy.Decision(False, policy.FAILED, per_account, watch)
    with _lock:
        say_raised = raised is not None and raised != _raised
        _raised = raised
        changed = _last is None or (d.relay_on, d.reason) != (_last.relay_on, _last.reason)
        _last = d
        if changed:
            EVENTS.append({"at": int(now), "relay_on": d.relay_on, "reason": d.reason})
    if say_raised:
        print(f"[routing] the quota rule raised {raised}", file=sys.stderr, flush=True)
    if changed:
        print(f"[routing] relay {'on' if d.relay_on else 'off'}: {d.reason}", file=sys.stderr, flush=True)
    # The models are Auto's rule: Relay cuts every request and Direct passes every one, whatever `models` holds.
    return policy.for_model(d, config.LOW_QUOTA, model) if mode == "auto" and model is not ... else d
