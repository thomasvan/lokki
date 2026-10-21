#!/usr/bin/env python3
"""Billing oracle for the midstream-abort relay.

`runs.jsonl` records `verdict: usage-discarded`, which only means the upstream
never emitted a terminal frame. That is the precondition for the cut, not proof
that the request cost nothing. This tool reads the upstream's own usage meter
instead, so a billing claim becomes an operator-checkable measurement rather
than a dashboard observation only one person can make.

Two commands, both of which spend no quota:

  read [--record LABEL --note TEXT]
      Print every codex account's 5h and weekly used-percent, both as cliproxy
      cached it from response headers and as the upstream reports it right now.
      The cached copy is what the management panel shows; the live copy is what
      brackets a burst. `--record` appends the reading to `billing_reads.jsonl`
      under LABEL, which is how a delayed re-read gets recorded rather than
      transcribed by hand — and how a measurement is made from two readings
      rather than from one.

  tokens [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--bucket hour|day|month] [--json]
      Print what the cpa-usage plugin recorded per request: request count,
      input/output/reasoning tokens, and how much of the input the provider
      served from cache, grouped by the plugin's anonymised account key and by
      model. This is the token side of the meter's percent. It spends no quota:
      it only reads the plugin's management API, never the upstream.

      The plugin hashes the account on the way out, so the groups are
      `key-<hash>` and stable per account, not emails. The email behind a group
      lives in the plugin's SQLite, which is where the per-account split in
      `billing_arms_2026-10-04.json` came from.  An empty event list *with* a
      `collect_err` is a broken oracle, not an idle pool — that is the failure
      mode the plugin shipped with (docs/relay/usage-oracle.md) — so this command
      raises rather than printing zeros.

`billing_ab.jsonl` and `billing_arms_2026-10-04.json` are the committed record of
the arms that were run with `ab`, a subcommand that bracketed N relay requests
between two meter reads. `ab` sent live requests and was removed with the rest of
the research tooling; the readings it produced stay, and a caller who wants a new
arm writes the two `read --record` lines around its own traffic.

Limits worth stating in any document that cites the output:

  * `used_percent` is an integer, so 1% is the resolution. "0%" after a burst
    means "< 1% of the window over that many requests", not literal zero.
  * The meter is the upstream's. It is authoritative about billing but it says
    nothing about which request caused a change; only the rate matters.
  * Do not count `response.completed` frames to tell the arms apart. `segmented`
    emits a synthetic one on purpose, so the frame type is identical in both
    arms; the record therefore carries `real_completed` (the relay's assertion)
    and `synthetic_completed` separately, and the meter delta is the arbiter.
  * Quota reads go through /v8/management/requests/api-call, which spends no
    quota: it is a plain GET to the usage endpoint.

Environment:
  CLIPROXY_MGMT         default http://127.0.0.1:8317
  CLIPROXY_MGMT_SECRET  required; the management password
  RELAY_BASE            default http://127.0.0.1:8371
  RELAY_TOKEN             relay token; otherwise read from core/config.yaml
  RELAY_MODEL           default gpt-6-astra
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import yaml
except ImportError:  # fall back to a line parse, as relay.config does
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
MGMT = os.environ.get("CLIPROXY_MGMT", "http://127.0.0.1:8317").rstrip("/")
MGMT_SECRET = os.environ.get("CLIPROXY_MGMT_SECRET", "")
RELAY = os.environ.get("RELAY_BASE", "http://127.0.0.1:8371").rstrip("/")
MODEL = os.environ.get("RELAY_MODEL", "gpt-6-astra")
READS_EVIDENCE = os.path.join(HERE, "billing_reads.jsonl")
RELAY_CONFIG = os.path.join(HERE, "config.yaml")

# One home: the reader.
sys.path.insert(0, HERE)
from lokki_core.quota.reader import USAGE_URL as WHAM_USAGE, USAGE_HEADERS as WHAM_HEADERS  # noqa: E402

APOLOGY = {
    "product_usage": "The usage endpoint answered without a rate_limit block. "
                     "This plan may not meter the same way; do not read a "
                     "missing percent as zero.",
}


class ProbeError(Exception):
    """A configuration or transport problem the operator has to fix."""


def _request(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.read()


def _mgmt_headers():
    if not MGMT_SECRET:
        raise ProbeError(
            "CLIPROXY_MGMT_SECRET is unset. Set it to the management password; "
            "this tool never reads it from a file, so it cannot leak into git.")
    return {"Authorization": f"Bearer {MGMT_SECRET}", "Content-Type": "application/json"}


def mgmt_get(path):
    status, body = _request(MGMT + path, headers=_mgmt_headers())
    if status != 200:
        raise ProbeError(f"GET {path} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def mgmt_post(path, payload):
    status, body = _request(MGMT + path, data=json.dumps(payload).encode(),
                            headers=_mgmt_headers(), method="POST")
    if status != 200:
        raise ProbeError(f"POST {path} -> HTTP {status}: {body[:200]!r}")
    return json.loads(body)


def _first_token(entries, operator_only=False):
    """The first usable token in a list of {name, token, operator?} entries.

    `operator_only` matters for an arm: one that drives `/api/run` with small
    explicit budgets needs the knobs those budgets name, and since knob rights
    follow the identity, a *client* token there would silently run the operator's
    default policy instead — a slower arm that measures the same thing, but not
    what the code says it asks for."""
    fallback = None
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        token = str(entry.get("token") or "").strip()
        if not token:
            continue
        if entry.get("operator"):
            return token
        fallback = fallback or token
    return None if operator_only else fallback


def relay_token(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("RELAY_TOKEN"):
        return os.environ["RELAY_TOKEN"]
    try:
        env_tokens = json.loads(os.environ.get("RELAY_TOKENS") or "null")
    except ValueError:
        env_tokens = None
    if isinstance(env_tokens, list):
        token = _first_token(env_tokens)
        if token:
            return token
    if yaml is not None:
        try:
            with open(RELAY_CONFIG) as f:
                doc = yaml.safe_load(f) or {}
            auth = doc.get("auth") or {}
            token = auth.get("token")
            if token:
                return str(token).strip()
            # W0: the relay's config may name its clients in a list instead. This
            # tool is the operator's own, so an operator token is used when there
            # is one — otherwise an arm's budgets would be ignored and it would
            # quietly measure a different policy. Any client's token still
            # authenticates the read when no operator is configured.
            token = _first_token(auth.get("tokens"), operator_only=True)
            if token:
                return token
            token = _first_token(auth.get("tokens"))
            if token:
                return token
        except (OSError, AttributeError):
            pass
    # Without PyYAML, fall back to a line parse. It can be fooled by a commented
    # or nested `token:`, which is why the loader is tried first.
    try:
        with open(RELAY_CONFIG) as f:
            for line in f:
                if line.lstrip().startswith("token:"):
                    return line.split(":", 1)[1].strip().strip("'\"")
    except OSError as e:
        raise ProbeError(f"cannot read {RELAY_CONFIG}: {e}")
    raise ProbeError(
        f"no client token in {RELAY_CONFIG} and none in the environment: set "
        "auth.token or auth.tokens in that file, or RELAY_TOKEN/RELAY_TOKENS")


def codex_accounts():
    """(auth_index, email, cached_signals) for every codex credential."""
    out = []
    for entry in mgmt_get("/v8/management/credentials").get("files", []):
        if entry.get("provider") != "codex":
            continue
        signals = (entry.get("quota") or {}).get("signals") or {}
        out.append((entry.get("auth_index", ""), entry.get("email", "?"), signals))
    if not out:
        raise ProbeError("no codex credentials are configured in cliproxy")
    return out


def live_usage(auth_index):
    """Ask the upstream for one account's meter. Spends no quota."""
    resp = mgmt_post("/v8/management/requests/api-call", {
        "authIndex": auth_index, "method": "GET", "url": WHAM_USAGE,
        "header": dict(WHAM_HEADERS),
    })
    status = resp.get("status_code")
    body = resp.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None
    if status != 200 or not isinstance(body, dict):
        return {"error": f"HTTP {status}"}
    rl = body.get("rate_limit") or {}
    return {
        "plan": body.get("plan_type"),
        "primary_percent": (rl.get("primary_window") or {}).get("used_percent"),
        "secondary_percent": (rl.get("secondary_window") or {}).get("used_percent"),
        "primary_reset_at": (rl.get("primary_window") or {}).get("reset_at"),
        "secondary_reset_at": (rl.get("secondary_window") or {}).get("reset_at"),
        "limit_reached": body.get("rate_limit_reached_type"),
        "credits": body.get("credits"),
    }


def snapshot():
    """One meter reading per account: cached copy plus live upstream copy."""
    out = {}
    for auth_index, email, signals in codex_accounts():
        out[email] = {
            "cached_primary_percent": signals.get("X-Codex-Primary-Used-Percent"),
            "cached_secondary_percent": signals.get("X-Codex-Secondary-Used-Percent"),
            "observed_at": signals.get("observed_at"),
            "live": live_usage(auth_index),
        }
    return out


def _fmt(snap):
    lines = []
    for email, s in sorted(snap.items()):
        live = s["live"]
        lines.append(
            f"  {email:42s} plan={live.get('plan')!s:6s} "
            f"5h={live.get('primary_percent')}% weekly={live.get('secondary_percent')}% "
            f"(cached 5h={s['cached_primary_percent']}% "
            f"weekly={s['cached_secondary_percent']}%)")
    return "\n".join(lines)


def missing_windows(snap):
    """Accounts whose live read came back without a meter value."""
    return [email for email, s in snap.items()
            if (s.get("live") or {}).get("primary_percent") is None]


def warn_missing(*snaps):
    """A read that failed is not a read that showed no change. Say so out loud:
    a null delta in the evidence file is only unambiguous to someone who knows
    to look for it."""
    missing = sorted({email for snap in snaps for email in missing_windows(snap)})
    if missing:
        print(f"WARNING: {len(missing)} account(s) returned no primary window in "
              f"a live read; their deltas are recorded as null, NOT as zero. "
              + APOLOGY["product_usage"])


def cmd_read(args):
    snap = snapshot()
    print(_fmt(snap))
    warn_missing(snap)
    if args.record:
        labels = label_map(snap)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "revision": revision(),
            "relay": RELAY,
            "label": args.record,
            "note": args.note or "",
            "snapshot": relabel(snap, labels),
        }
        path = append_evidence(record, args.evidence or READS_EVIDENCE)
        print(f"evidence appended to {path}", flush=True)
    return 0


PLUGIN_USAGE_PATH = "/v0/management/plugins/usage-report/api/usage"


def plugin_usage(start=None, end=None, bucket="hour"):
    """The cpa-usage plugin's per-request record. Spends no quota.

    `collect_err` is the only field that says whether the plugin is collecting at
    all. A plugin that cannot open its database answers 200 with an empty event
    list, which reads exactly like an idle pool; that is an error here, not a
    zero. See docs/relay/usage-oracle.md.
    """
    query = {"bucket": bucket}
    if start:
        query["start"] = start
    if end:
        query["end"] = end
    data = mgmt_get(PLUGIN_USAGE_PATH + "?" + urllib.parse.urlencode(query))
    if not isinstance(data, dict) or "events" not in data:
        raise ProbeError(f"the usage plugin answered {str(data)[:200]!r}")
    if data.get("collect_err"):
        raise ProbeError("the usage plugin is not collecting: "
                         f"{data['collect_err']} (see docs/relay/usage-oracle.md)")
    return data


def _event_tokens(event):
    """(input, cache_read, output, reasoning) from one plugin event.

    The management API flattens the token counts into the event itself; the
    plugin's SQLite payload nests them under `tokens`. Accept both, because this
    reads a shape named by a plugin we do not control.
    """
    if "input" in event or "output" in event:
        return (int(event.get("input") or 0), int(event.get("cache") or 0),
                int(event.get("output") or 0), int(event.get("reasoning") or 0))
    detail = event.get("tokens") or {}
    cache = int(detail.get("cache_read_tokens") or 0) or int(detail.get("cached_tokens") or 0)
    return (int(detail.get("input_tokens") or 0), cache,
            int(detail.get("output_tokens") or 0), int(detail.get("reasoning_tokens") or 0))


def token_totals(events):
    """Aggregate plugin events into totals, per anonymised key and per model.

    `uncached` is the axis the 2026-10-04 arms turned on: the charge follows
    input the provider did not serve from cache, so a report that printed only
    `input` would hide the number that matters.
    """

    def blank():
        return {"requests": 0, "input": 0, "cache": 0, "uncached": 0,
                "output": 0, "reasoning": 0, "failed": 0}

    def add(row, inp, cache, out, reason, failed):
        row["requests"] += 1
        row["input"] += inp
        row["cache"] += cache
        row["uncached"] += max(inp - cache, 0)
        row["output"] += out
        row["reasoning"] += reason
        row["failed"] += 1 if failed else 0

    total, keys, models = blank(), {}, {}
    for event in events:
        inp, cache, out, reason = _event_tokens(event)
        failed = bool(event.get("failed"))
        add(total, inp, cache, out, reason, failed)
        for table, name in ((keys, event.get("key") or "unknown"),
                            (models, event.get("model") or "unknown")):
            add(table.setdefault(name, blank()), inp, cache, out, reason, failed)
    return {"total": total, "by_key": keys, "by_model": models}


def _tokline(label, row):
    line = (f"{label:28} {row['requests']:>5} reqs  in {row['input']:>10,} "
            f"(cached {row['cache']:>10,}, uncached {row['uncached']:>9,})  "
            f"out {row['output']:>8,}  reasoning {row['reasoning']:>7,}")
    if row["failed"]:
        line += f"  failed {row['failed']}"
    return line


def cmd_tokens(args):
    data = plugin_usage(args.start, args.end, args.bucket)
    totals = token_totals(data.get("events") or [])
    if args.json:
        print(json.dumps({
            "window": {"start": args.start, "end": args.end, "bucket": args.bucket},
            "collected_at": data.get("collected_at"),
            "skipped": data.get("skipped"),
            **totals,
        }, indent=2, sort_keys=True))
        return 0
    total = totals["total"]
    print(f"usage plugin: {total['requests']} requests "
          f"(bucket={args.bucket}, start={args.start or 'plugin default'}, "
          f"end={args.end or 'plugin default'})")
    print(_tokline("  all", total))
    for name, row in sorted(totals["by_key"].items(), key=lambda kv: -kv[1]["input"]):
        print(_tokline(f"  key {name}", row))
    for name, row in sorted(totals["by_model"].items(), key=lambda kv: -kv[1]["input"]):
        print(_tokline(f"  model {name}", row))
    print("  (the plugin's record, not the meter: quota is `read`)")
    return 0



def label_map(snap):
    """Stable per-record labels so account emails never reach the evidence file."""
    return {email: f"acct-{i}" for i, email in enumerate(sorted(snap), 1)}


def relabel(mapping, labels):
    out = {}
    for email, value in mapping.items():
        entry = dict(value) if isinstance(value, dict) else value
        if isinstance(entry, dict):
            entry["label"] = labels.get(email, email)
        out[labels.get(email, email)] = entry
    return out


def revision():
    import subprocess
    try:
        return subprocess.check_output(["git", "-C", HERE, "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


def append_evidence(record, path=None):
    """Append one reading to the evidence file, never overwriting a line.

    `path` defaults to the reads file: the arm file (`billing_ab.jsonl`) is
    committed evidence of measurements that have been taken, and nothing here
    writes to it any more."""
    path = path or READS_EVIDENCE
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_read = sub.add_parser("read", help="print the current meter for every account")
    p_read.add_argument("--record", default=None, metavar="LABEL",
                        help="append this reading to the evidence file under LABEL, "
                             "so a delayed re-read is recorded rather than "
                             "transcribed by hand")
    p_read.add_argument("--note", default="",
                        help="what makes this reading meaningful, e.g. the burst it "
                             "follows and the traffic that did not happen since")
    p_read.add_argument("--evidence", default=None, help="evidence file to append to")
    p_read.set_defaults(func=cmd_read)

    p_tokens = sub.add_parser(
        "tokens", help="what the cpa-usage plugin recorded per request")
    p_tokens.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                          help="the plugin buckets in its own local timezone "
                               "(CST here), so pass dates explicitly when a "
                               "window has to line up with a meter read")
    p_tokens.add_argument("--end", default=None, metavar="YYYY-MM-DD")
    p_tokens.add_argument("--bucket", default="hour",
                          choices=["hour", "day", "month"])
    p_tokens.add_argument("--json", action="store_true",
                          help="machine-readable totals instead of the table")
    p_tokens.set_defaults(func=cmd_tokens)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
