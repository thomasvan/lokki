"""The mode's decision: one pure function of a snapshot, the settings, the clock and
the last read's error. Called per request, so a stalled poller cannot leave a stale
decision in force. A quota that cannot be read counts as not low."""
from dataclasses import dataclass, field

FIVE_HOURS = 18000


# Every reason a decision gives, named once here and returned by these names alone, in the
# order of the spec's table. `docs/app/api.md` lists them, and the tests hold that list and the
# reasons the rows return equal to `REASONS`.
RELAY = "routing: relay"
DIRECT = "routing: direct"
CANNOT_READ = "the quota cannot be read"
NOT_READ_YET = "the quota is not read yet"
NONE_WATCHED = "no account to watch"
NONE_READ = "no watched account could be read"
ALL_LOW = "every watched account is low"
ONE_UNREAD = "a watched account could not be read"
NOT_ALL_LOW = "not every watched account is low"
ONE_LOW = "a watched account is low"
NONE_LOW = "no watched account is low"
UNNAMED = "this model is not cut when low"
FAILED = "the quota rule failed"

REASONS = (RELAY, DIRECT, CANNOT_READ, NOT_READ_YET, NONE_WATCHED, NONE_READ, ALL_LOW, ONE_UNREAD,
           NOT_ALL_LOW, ONE_LOW, NONE_LOW, UNNAMED, FAILED)


@dataclass(frozen=True)
class Decision:
    relay_on: bool
    reason: str
    per_account: dict = field(default_factory=dict)
    watched: frozenset = frozenset()


def state_of(account, now, stale, threshold):
    """"low" | "ok" | "unknown" for one account; `stale` is whether its snapshot is too old to trust."""
    used = account.get("used_percent")
    if stale or account.get("error") or not isinstance(used, (int, float)) or isinstance(used, bool):
        return "unknown"
    window = account.get("window_seconds")
    if window is not None and window != FIVE_HOURS:
        return "unknown"
    reset_at = account.get("reset_at")
    remaining = 100 if isinstance(reset_at, (int, float)) and 0 < reset_at <= now else 100 - used
    return "low" if remaining < threshold else "ok"


def states(snapshot, low_quota, now):
    """The state of every account of a snapshot, watched or not; {} without one."""
    if not snapshot:
        return {}
    stale = (now - float(snapshot.get("read_at") or 0)) > 3 * low_quota["poll_seconds"]
    return {i: state_of(a, now, stale, low_quota["threshold_percent"])
            for i, a in (snapshot.get("accounts") or {}).items()}


def watched(snapshot, low_quota):
    """The indexes Auto watches: the named ones the snapshot holds when `low_quota.accounts`
    names any, else its enabled accounts."""
    if not snapshot:
        return frozenset()
    accounts = snapshot.get("accounts") or {}
    scope = list(low_quota.get("accounts") or ())
    if scope:
        return frozenset(i for i in accounts if i in scope)
    return frozenset(i for i, a in accounts.items() if a.get("enabled"))


def fixed(routing, per_account, watched):
    """The decision `relay` and `direct` make, whatever the quota says; None for `auto`."""
    if routing == "relay":
        return Decision(True, RELAY, per_account, watched)
    if routing == "direct":
        return Decision(False, DIRECT, per_account, watched)
    return None


def decide(snapshot, low_quota, routing, now, read_error, per_account=None, watch=None):
    """`read_error` is the poller's last error, None when the last read answered; it decides
    only without a snapshot (staleness already ages one). `per_account` and `watch` are
    `states` and `watched` of the same snapshot, for a caller that already holds them."""
    if per_account is None:
        per_account = states(snapshot, low_quota, now)      # shown whatever is decided below
    if watch is None:
        watch = watched(snapshot, low_quota)
    chosen = fixed(routing, per_account, watch)
    if chosen:
        return chosen

    def said(relay_on, reason):
        return Decision(relay_on, reason, per_account, watch)

    if not snapshot:
        return said(False, CANNOT_READ if read_error else NOT_READ_YET)
    seen = [s for i, s in per_account.items() if i in watch]
    if not seen:
        return said(False, NONE_WATCHED)
    if all(s == "unknown" for s in seen):
        return said(False, NONE_READ)
    if low_quota.get("rule") == "any":
        if "low" in seen:
            return said(True, ONE_LOW)
        if "unknown" in seen:
            return said(False, ONE_UNREAD)
        return said(False, NONE_LOW)
    if "ok" in seen:
        return said(False, NOT_ALL_LOW)
    if "unknown" in seen:
        return said(False, ONE_UNREAD)
    return said(True, ALL_LOW)


def _without_suffix(model):
    """`model` with CLIProxyAPI's one thinking-level suffix removed, as its `ParseSuffix` does: the
    text before the last `(` when the model ends with `)`; else `model` itself."""
    if model.endswith(")") and "(" in model:
        return model[:model.rindex("(")]
    return model


def for_model(decision, low_quota, model):
    """The request's decision: a decision that cuts passes through when `low_quota.models`
    names models and `model` is not one of them. CLIProxyAPI reads `gpt-5.5(high)` as `gpt-5.5`
    at a thinking level, and the picker offers plain ids, so a model is named when it, or its
    name with that one suffix removed, is in the list. A missing or non-string model is not
    named. Auto's rule: the caller (`routing.decide_now`) applies it under Auto only."""
    models = low_quota.get("models")
    if not decision.relay_on or not models:
        return decision
    if isinstance(model, str) and (model in models or _without_suffix(model) in models):
        return decision
    return Decision(False, UNNAMED, decision.per_account, decision.watched)
