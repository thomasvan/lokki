"""Writes the e2e's run log: records over the 72 hours before --now and none earlier, so the 24 h
window and the 7 d window (whose history then starts inside it) both have bars to draw.

Every route, every relay class, errors on each route, eleven models (ten chat, one image) and two clients (so the top-8
fold shows). Each record holds only fields its writer writes (engine.run_upstream,
passthrough.serve, images.new_record) with sample values, chained as core.log_run chains them and
written the way it writes them (json.dumps(rec, ensure_ascii=False), the default separators), so
the core's reads go through the same bytes a real log holds.

    python3 seed_runs.py --out PATH --now EPOCH
"""
import argparse
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "core"))
from lokki_core import core  # noqa: E402  (the one home of the record hash)

HOURS = 72
MODELS = [f"gpt-sample-{i}" for i in range(10)]
CLIENTS = ["sample-client-a", "sample-client-b"]
ROUTES = ["relay"] * 11 + ["passthrough"] * 7 + ["images"]
CUTS = ["marker", "after-deltas", "cap", "toolcall", "eof", "budget"]
USAGE = {"input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540, "cached_tokens": 800,
         "input_tokens_details": {"cached_tokens": 800}, "output_tokens_details": {"reasoning_tokens": 60}}


def relay(rng, errored):
    cls = "error" if errored else rng.choice(["finalized", "finalized", "cut", "cut", "cut", "client_gone"])
    segs = rng.randint(1, 4)
    # As engine.py writes them: `cuts` lists each segment's ending, `cut` is the last one, `client_gone` a sentence or None.
    cuts = [rng.choice(CUTS) for _ in range(segs - 1)]
    last = {"finalized": "terminal", "cut": rng.choice(CUTS), "client_gone": "client_gone", "error": "error"}[cls]
    rec = {"route": "relay", "mode": "segmented", "model": rng.choice(MODELS), "client": rng.choice(CLIENTS),
           "cut": last, "cuts": cuts + [last], "real_completed": cls == "finalized",
           "verdict": "billed" if cls == "finalized" else "usage-discarded", "truncated": cls == "cut",
           "segs": segs, "delivered_chars": rng.randint(200, 4000), "elapsed_s": round(rng.uniform(1.5, 40), 2),
           "error": None, "upstream_error": None, "upstream_status": 200, "usage": USAGE if cls == "finalized" else None,
           "client_gone": "the client left while the relay closed the stream" if cls == "client_gone" else None}
    if cls == "error":
        rec.update(error="upstream closed the stream", upstream_status=502)
    return rec


def passthrough(rng, errored):
    rec = {"route": "passthrough", "reason": rng.choice(["routing: direct", "not every account in scope is low"]),
           "model": rng.choice(MODELS), "client": rng.choice(CLIENTS), "stream": True, "status": 200, "cut": "terminal",
           "error": None, "usage": USAGE, "elapsed_s": round(rng.uniform(1, 60), 2)}
    if errored:
        rec.update(status=429, cut="error", error="HTTP 429", usage=None)
    return rec


def image(rng, errored):
    rec = {"route": "images", "model": "gpt-image-sample", "client": rng.choice(CLIENTS), "status": 200, "images_out": 1,
           "error": None, "upstream_error": None, "elapsed_s": round(rng.uniform(8, 50), 2)}
    if errored:
        rec.update(status=400, images_out=0, upstream_error={"status": 400, "code": "invalid_request", "type": "invalid_request_error"})
    return rec


def build(now):
    rng = random.Random(14)
    stamps = []
    for back in range(HOURS):
        # A busier recent day, a quiet hour here and there.
        n = 0 if rng.random() < 0.15 else rng.randint(1, 9 if back < 24 else 5)
        hour = now - (back + 1) * 3600
        stamps += [hour + rng.randrange(3600) for _ in range(n)]
    stamps.sort()
    made = {"relay": relay, "passthrough": passthrough, "images": image}
    out, tally = [], {r: 0 for r in made}
    for ts in stamps:
        route = rng.choice(ROUTES)
        tally[route] += 1
        errored = tally[route] % 7 == 0           # errors on every route, in the last day too
        rec = made[route](rng, errored)
        rec.update(ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), revision="seeded0", config_fingerprint="0123456789ab")
        out.append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--now", type=int, required=True)
    args = ap.parse_args()
    prev, lines = core.GENESIS_HASH, []
    for seq, rec in enumerate(build(args.now), start=1):
        rec["seq"], rec["prev_hash"] = seq, prev
        rec["hash"] = prev = core.record_hash(rec)
        lines.append(json.dumps(rec, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))
    print(len(lines))


if __name__ == "__main__":
    main()
