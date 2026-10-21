import { vi } from "vitest";
import type { Account, Accounts, Activity, ActivityWindow, SchemaRow, SettingsView, Status } from "./lib/api";
import type { Routing } from "./lib/mode";
import { app } from "./lib/state.svelte";
import schema from "./views/schema.fixture.json";

// A fresh Response per call: a Response body can be read once, so a mock that
// hands the same object to two calls fails the second with "Body is unusable".
export function answers(...list: Array<[number, unknown] | Error>) {
  const fetch = vi.fn();
  for (const item of list) {
    if (item instanceof Error) fetch.mockRejectedValueOnce(item);
    else fetch.mockImplementationOnce(() => Promise.resolve(new Response(
      item[1] === undefined ? null : JSON.stringify(item[1]),
      { status: item[0], headers: { "content-type": "application/json" } })));
  }
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

// Answers by path, for code that fetches several routes at once. An unlisted path is a 404.
export function routes(table: Record<string, unknown>) {
  const fetch = vi.fn((path: string) => Promise.resolve(path in table
    ? new Response(JSON.stringify(table[path]), { status: 200, headers: { "content-type": "application/json" } })
    : new Response(null, { status: 404 })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

export const callsTo = (fetch: ReturnType<typeof vi.fn>, path: string) =>
  fetch.mock.calls.filter((c) => c[0] === path).length;

// A core that never answers. `honoringAbort` rejects when the request's signal fires, as a real fetch does.
export function hangs(honoringAbort = true) {
  const fetch = vi.fn((_path: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    if (honoringAbort) init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

export function statusFixture(over: Partial<Status> = {}): Status {
  return {
    revision: "abc1234", version: "0.1.0", started_at: "2026-10-11T00:00:00Z", uptime_s: 5,
    routing: "auto",
    low_quota: { threshold_percent: 30, rule: "all", accounts: [], models: [], poll_seconds: 60 },
    decision: { relay_on: false, reason: "not every watched account is low" },
    poll: { last_ok_at: null, last_error: null, reads: 0, running: true },
    runs_since_start: {},
    cliproxy: { url: "http://127.0.0.1:8318", reachable: true, version: null },
    unknown_keys: [],
    ...over,
  };
}

export const accountsFixture = (over: Partial<Accounts> = {}): Accounts => ({
  read_at: 1_800_000_000, age_s: 3, accounts: [], decision: statusFixture().decision, poll: statusFixture().poll, ...over,
});

export const accountFixture = (over: Partial<Account> = {}): Account => ({
  auth_index: "a1", label: "a1", email_masked: "o***@example.invalid", enabled: true, used_percent: 80, remaining_percent: 20,
  reset_at: 4_000_000_000, state: "low", error: null, in_scope: true, ...over,
});

// GET /api/activity as docs/app/api.md words it: 24 hourly buckets or 28 six-hourly ones ending at `now`,
// every count at zero unless `buckets` overrides one (by index) and `over` the rest.
export function activityFixture(over: Partial<Activity> = {}, buckets: Record<number, Partial<Activity["buckets"][number]>> = {}, window: ActivityWindow = "24h"): Activity {
  const n = window === "24h" ? 24 : 28;
  const step = window === "24h" ? 3600 : 6 * 3600;
  const now = 1_800_000_000;
  const from = now - n * step;
  const list = Array.from({ length: n }, (_, k) => ({ start: from + k * step, relay: 0, passthrough: 0, images: 0, errors: 0, ...buckets[k] }));
  const sum = (key: "relay" | "passthrough" | "images" | "errors") => list.reduce((a, b) => a + b[key], 0);
  const totals = { requests: sum("relay") + sum("passthrough") + sum("images"), relay: sum("relay"), passthrough: sum("passthrough"), images: sum("images"), errors: sum("errors") };
  const nobody = { p50: null, p90: null };
  return {
    window, now, from, coverage: { oldest: from, records: totals.requests, undated: 0, skipped: 0, complete: true },
    buckets: list, totals,
    relay: { finalized: 0, cut: 0, client_gone: 0, error: 0, by_cut: {} }, passthrough: { by_reason: {}, by_cut: {} },
    tokens: { runs: 0, input: 0, output: 0, cached: 0, reasoning: 0 }, models: [], clients: [],
    latency: { relay: nobody, passthrough: nobody, images: nobody, all: nobody }, recent: [], events: [], ...over,
  };
}

// The real listing of `python3 -m lokki_core schema`; the e2e compares it to the live /api/schema.
export const schemaRows = schema as unknown as SchemaRow[];

// GET /api/settings as the core builds it: every row at its default, nested by
// key prefix, no auth.* (the core adds a masked token list the UI never shows).
export function settingsFixture(over: Partial<SettingsView> = {}): SettingsView {
  const view: Record<string, unknown> = {};
  for (const row of schemaRows) {
    if (row.key.startsWith("auth.")) continue;
    const [head, rest] = row.key.split(".");
    if (rest === undefined) view[head] = row.default;
    else view[head] = { ...(view[head] as object), [rest]: row.default };
  }
  return { ...view, env_overrides: [], writable: true, unknown_keys: [], ...over } as SettingsView;
}

// A core for the mode writes: each PUT applies its routing and threshold to what the status reports. `hold`
// keeps each PUT unanswered until `release` answers the oldest; `refuse` answers the next PUTs, in order, with
// a status and an envelope, or a network failure; `statusFails` fails that many status reads.
export function modeCore(routing: Routing = "auto", threshold = 30) {
  const state = { routing, threshold };
  const ctl = { hold: false, refuse: [] as Array<readonly [number, unknown] | "network">, statusFails: 0, puts: [] as string[] };
  const held: Array<() => void> = [];
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  const report = (): Status => statusFixture({ routing: state.routing, low_quota: { ...statusFixture().low_quota, threshold_percent: state.threshold } });
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      const sent = String(init.body);
      ctl.puts.push(sent);
      const answer = (): Promise<Response> => {
        const next = ctl.refuse.shift();
        if (next === "network") return Promise.reject(new TypeError("Failed to fetch"));
        if (next) return Promise.resolve(json(...next));
        const body = JSON.parse(sent) as { routing?: Routing; low_quota?: { threshold_percent?: number } };
        if (body.routing) state.routing = body.routing;
        if (body.low_quota?.threshold_percent !== undefined) state.threshold = body.low_quota.threshold_percent;
        return Promise.resolve(json(200, { applied: Object.keys(body), restart_required: [], comments_dropped: false }));
      };
      if (!ctl.hold) return answer();
      return new Promise<Response>((done, fail) => { held.push(() => { answer().then(done, fail); }); });
    }
    if (path === "/api/settings") return Promise.resolve(json(200, settingsFixture()));
    if (path === "/api/status") {
      if (ctl.statusFails > 0) { ctl.statusFails--; return Promise.resolve(json(500, { error: { message: "the status could not be built" } })); }
      return Promise.resolve(json(200, report()));
    }
    if (path === "/api/accounts") return Promise.resolve(json(200, accountsFixture()));
    return Promise.resolve(new Response(null, { status: 404 }));
  });
  vi.stubGlobal("fetch", fetch);
  app.status = report();
  return { fetch, ctl, state, report, release: () => held.shift()?.(), waiting: () => held.length };
}
