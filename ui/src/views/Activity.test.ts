import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { activityFixture, statusFixture } from "../test-helpers";
import type { Activity as ActivityBody, RecentRun } from "../lib/api";
import { NOT_ANSWERING } from "../lib/api";
import { clock } from "../lib/format";
import { app, stopPolling } from "../lib/state.svelte";
import Activity from "./Activity.svelte";

const NOW = 1_800_000_000;
const HOUR = 3600;
const weekday = (epochS: number) => new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(new Date(epochS * 1000));
const iso = (epochS: number) => new Date(epochS * 1000).toISOString().replace(/\.\d+Z$/, "Z");
const flat = (el: Element) => (el.textContent ?? "").replace(/\s+/g, " ").trim();
const cells = (row: HTMLElement) => within(row).getAllByRole("cell").map(flat);
const LOCKED = "Cut runs get no token count from the provider, and these totals count none for them.";

function run(over: Partial<RecentRun> = {}): RecentRun {
  return {
    ts: NOW - 60, route: "relay", model: "gpt-5.1-codex", client: "default", mode: "relay", cut: "marker", verdict: "usage-discarded",
    outcome: "cut", status: 200, elapsed_s: 4.1, delivered_chars: 900, segs: 1, input_tokens: null, output_tokens: null,
    reason: null, error: null, ...over,
  };
}

// A 24 h answer with every part filled: the counts a window of 60 requests would give.
function full(over: Partial<ActivityBody> = {}, window: "24h" | "7d" = "24h"): ActivityBody {
  return activityFixture({
    relay: { finalized: 3, cut: 48, client_gone: 1, error: 2, by_cut: { marker: 28, "after-deltas": 12, budget: 8 } },
    passthrough: { by_reason: { "not every account in scope is low": 3, "routing: direct": 2 }, by_cut: { terminal: 4, error: 1 } },
    tokens: { runs: 8, input: 380_000, output: 26_000, cached: 290_000, reasoning: 4_000 },
    models: [{ model: "gpt-5.1-codex", runs: 40 }, { model: "gpt-5.1", runs: 19 }, { model: "other", runs: 1 }],
    clients: [{ client: "default", runs: 58 }, { client: "laptop", runs: 2 }],
    latency: { relay: { p50: 4.1, p90: 11.8 }, passthrough: { p50: 6.3, p90: 14 }, images: { p50: null, p90: null }, all: { p50: 4.3, p90: 12.1 } },
    recent: [run({ ts: NOW - 60 }), run({ ts: NOW - 120, outcome: "finalized", verdict: "billed", cut: "eof", output_tokens: 1204 })],
    events: [
      { at: NOW - 600, relay_on: true, reason: "every account in scope is low" },
      { at: NOW - 3 * HOUR, relay_on: false, reason: "no snapshot yet" },
    ],
    ...over,
  }, { 21: { relay: 16, errors: 1 }, 22: { relay: 21, passthrough: 2, images: 1 }, 23: { relay: 17 } }, window);
}

const answered = (a: ActivityBody) => { app.activity = a; app.activityWindow = a.window; };
const advance = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms); await tick(); };

describe("Activity", () => {
  beforeEach(() => {
    Object.assign(app, { status: statusFixture({ started_at: iso(NOW - 5 * HOUR) }), endpoint: null, accounts: null, error: null, busy: false, activity: null, activityWindow: "24h", activityBusy: false });
  });
  afterEach(() => { cleanup(); stopPolling(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("reads the window it is asked for, and draws only that window's answer", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW * 1000);
    answered(full());
    let release: () => void = () => {};
    const fetch = vi.fn((path: string) => new Promise<Response>((resolve) => {
      release = () => resolve(new Response(JSON.stringify(path.endsWith("7d") ? full({}, "7d") : full()), { status: 200 }));
    }));
    vi.stubGlobal("fetch", fetch);
    render(Activity, { ontab: () => {} });
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(24);

    await fireEvent.click(screen.getByRole("button", { name: "7 d" }));
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/activity?window=7d"]);
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(0);            // the 24 h answer in the slot draws nothing
    expect(screen.getByText("Reading the run log…")).toBeInTheDocument();

    release();
    await waitFor(() => expect(app.activity?.window).toBe("7d"));
    await tick();
    expect(document.querySelectorAll(".chart-x i")).toHaveLength(7);               // every fourth of 28 buckets
    expect(document.querySelectorAll(".chart-x i")[0].textContent).toMatch(new RegExp(`^${weekday(app.activity!.buckets[0].start)} `));
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("says what the counts cover", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW * 1000);
    const base = full();
    const sentence = (a: ActivityBody) => {
      cleanup();
      answered(a);
      render(Activity, { ontab: () => {} });
      return screen.queryByText(/^(counted )?since .*(run log)/)?.textContent ?? null;
    };
    const inside = base.from + 5 * HOUR;
    expect(sentence({ ...base, coverage: { ...base.coverage, oldest: inside } })).toBe(`since ${clock(inside)}: the run log holds runs since then`);
    expect(sentence({ ...base, coverage: { ...base.coverage, oldest: inside, complete: false } }))
      .toBe(`counted since ${clock(inside)}: lokki reads the last 8 MiB of its run log; older runs in this window are not counted`);
    expect(sentence({ ...base, coverage: { ...base.coverage, oldest: base.from - 5 * HOUR } })).toBeNull();      // complete, and the log reaches back past the window
    expect(sentence({ ...base, coverage: { ...base.coverage, oldest: base.from } })).toBeNull();
    const old = NOW - 30 * HOUR;
    const sevenDay = full({}, "7d");
    expect(sentence({ ...sevenDay, coverage: { ...sevenDay.coverage, oldest: old } })).toBe(`since ${weekday(old)} ${clock(old)}: the run log holds runs since then`);
  });

  it("keeps the chart's axes and says No requests yet on an empty window", async () => {
    answered(activityFixture());
    const ontab = vi.fn();
    render(Activity, { ontab });
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(24);
    expect(document.querySelectorAll("[data-axes] line")).toHaveLength(3);
    expect(screen.getAllByText("No requests yet").length).toBeGreaterThan(0);
    expect(screen.queryByRole("heading", { name: "Recent runs" })).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Open Home" }));
    expect(ontab).toHaveBeenCalledWith("home");
  });

  it("shows the outcomes, models, clients and latency of the answer", () => {
    answered(full());
    render(Activity, { ontab: () => {} });
    const kv = (label: string) => screen.getByText(label).closest("li")!;
    expect(kv("Cut by the relay before the provider finalized")).toHaveTextContent("48");
    expect(kv("Finalized, billed as a direct request")).toHaveTextContent("3");
    expect(kv("Client left")).toHaveTextContent("1");
    expect(kv("Error")).toHaveTextContent("2");
    expect(screen.getByRole("img", { name: "marker: 28 runs" })).toBeInTheDocument();          // How the cut runs ended
    expect(screen.getByRole("img", { name: "not every account in scope is low: 3 runs" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "terminal: 4 runs" })).toBeInTheDocument();
    const models = within(screen.getByRole("list", { name: "Models" })).getAllByRole("listitem").map(flat);
    expect(models).toEqual(["gpt-5.1-codex 40", "gpt-5.1 19", "other 1"]);
    expect(models.at(-1)).toBe("other 1");                                                      // the "other" row stays last
    expect(within(screen.getByRole("list", { name: "Clients" })).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("img", { name: "Relay: median 4.1 s, 90th percentile 11.8 s" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Images: no runs" })).toBeInTheDocument();
    expect(screen.getByText("no runs")).toBeInTheDocument();
    const tokens = screen.getByRole("region", { name: "Tokens the provider reported" });
    expect(tokens).toHaveTextContent("Input380 k");
    expect(tokens).toHaveTextContent("from 8 runs that reported usage");
    expect(tokens).toHaveTextContent(LOCKED);
  });

  it("keeps a very long model or client name on one line, with its full name in the title", () => {
    const longModel = "m".repeat(300);
    const longClient = "c".repeat(300);
    answered(full({
      models: [{ model: longModel, runs: 9 }], clients: [{ client: longClient, runs: 9 }],
      recent: [run({ model: longModel, client: longClient })],
    }));
    render(Activity, { ontab: () => {} });
    for (const name of [longModel, longClient]) {
      const titled = screen.getAllByTitle(name);
      expect(titled).toHaveLength(2);                                  // its breakdown row and its recent row
      for (const el of titled) {
        expect(el).toHaveTextContent(name);
        expect(el).toHaveClass("trunc");
      }
      expect(screen.getByRole("img", { name: `${name}: 9 runs` })).toBeInTheDocument();
      expect(screen.getByRole("cell", { name })).toBeInTheDocument();
    }
  });

  it("lists the recent runs with their outcome as given, and the decision changes newest first, the reasons verbatim, since lokki started", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW * 1000);
    answered(full({
      recent: [
        // cut and verdict would say "finalized"; the route said otherwise and the row shows what it was given
        run({ ts: NOW - 60, outcome: "client_gone", cut: "eof", verdict: "billed", status: 499, elapsed_s: 2.7 }),
        run({ ts: NOW - 120, route: "passthrough", outcome: "passed", reason: "routing: direct", cut: "terminal", verdict: null, output_tokens: 1204, elapsed_s: 12.6 }),
        run({ ts: NOW - 180, route: "images", outcome: "error", error: "HTTPError 502", status: 502, elapsed_s: 0.9 }),
      ],
    }));
    render(Activity, { ontab: () => {} });
    const rows = within(screen.getByRole("table", { name: "Recent runs" })).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    // time, route, model, client, outcome with its reason or error, status, duration, tokens out
    expect(cells(rows[0])).toEqual([clock(NOW - 60), "relay", "gpt-5.1-codex", "default", "client_gone", "499", "2.7 s", "—"]);
    expect(cells(rows[1])).toEqual([clock(NOW - 120), "pass-through", "gpt-5.1-codex", "default", "passed routing: direct", "200", "12.6 s", "1,204"]);
    expect(cells(rows[2])).toEqual([clock(NOW - 180), "images", "gpt-5.1-codex", "default", "error HTTPError 502", "502", "0.9 s", "—"]);
    expect(rows[2]).toHaveClass("err");
    expect(rows[0]).not.toHaveClass("err");

    const events = within(screen.getByRole("list", { name: "Decision changes" })).getAllByRole("listitem");
    expect(events.map(flat)).toEqual([
      `${clock(NOW - 600)} Cutting every request·every account in scope is low`,
      `${clock(NOW - 3 * HOUR)} Passing requests through·no snapshot yet`,
    ]);
    expect(screen.getByText(`since lokki started at ${clock(NOW - 5 * HOUR)}`)).toBeInTheDocument();
  });

  it("names no saving", () => {
    answered(full({ recent: [run({ outcome: "error", error: "HTTPError 502" })] }));
    render(Activity, { ontab: () => {} });
    const page = (document.body.textContent ?? "").replace(LOCKED, "");
    expect(document.body.textContent).toContain(LOCKED);
    expect(page).not.toMatch(/\bfree\b/i);
    expect(page).not.toMatch(/\bsav(e|ed|es|ing)\b/i);
    expect(page).not.toMatch(/[$€£]/);
  });

  it("moves every line that is relative to now as the clock advances, with no read and no remount", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW * 1000);
    const base = full();
    answered({
      ...base,
      coverage: { ...base.coverage, oldest: NOW - 23 * HOUR },
      recent: [run({ ts: NOW - 22 * HOUR })],
      events: [{ at: NOW - 22 * HOUR, relay_on: true, reason: "routing: relay" }],
    });
    app.status = statusFixture({ started_at: iso(NOW - 23 * HOUR) });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(Activity, { ontab: () => {} });
    const at = NOW - 23 * HOUR;
    expect(screen.getByText(`since ${clock(at)}: the run log holds runs since then`)).toBeInTheDocument();
    expect(screen.getByText(`since lokki started at ${clock(at)}`)).toBeInTheDocument();
    const firstTime = () => cells(within(screen.getByRole("table", { name: "Recent runs" })).getAllByRole("row")[1])[0];
    expect(firstTime()).toBe(clock(NOW - 22 * HOUR));

    await advance(2 * HOUR * 1000);                            // two hours on, all of them are more than a day old
    const dated = (t: number) => `${weekday(t)} ${clock(t)}`;
    expect(screen.getByText(`since ${dated(at)}: the run log holds runs since then`)).toBeInTheDocument();
    expect(screen.getByText(`since lokki started at ${dated(at)}`)).toBeInTheDocument();
    expect(firstTime()).toBe(dated(NOW - 22 * HOUR));
    expect(within(screen.getByRole("list", { name: "Decision changes" })).getAllByRole("listitem")[0]).toHaveTextContent(dated(NOW - 22 * HOUR));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("stops its clock when it unmounts", () => {
    vi.useFakeTimers();
    answered(full());
    const before = vi.getTimerCount();
    render(Activity, { ontab: () => {} });
    expect(vi.getTimerCount()).toBe(before + 1);              // the clock's tick
    cleanup();
    expect(vi.getTimerCount()).toBe(before);
  });

  it("says the read failed, in the error's words, until a read answers", async () => {
    app.activity = null;
    const core = vi.fn()
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockImplementation(() => Promise.resolve(new Response(JSON.stringify(full()), { status: 200 })));
    vi.stubGlobal("fetch", core);
    render(Activity, { ontab: () => {} });
    await fireEvent.click(screen.getByRole("button", { name: "7 d" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(`The run log could not be read: ${NOT_ANSWERING}`));
    expect(screen.queryByText("Reading the run log…")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "24 h" }));       // the next read answers
    await waitFor(() => expect(document.querySelectorAll("[data-bucket]")).toHaveLength(24));
    expect(screen.queryByText(/could not be read/)).toBeNull();
  });

  it("keeps the window choice enabled and busy while its read runs, keeps the focus, and lets the last choice win", async () => {
    vi.useFakeTimers();                                        // only to flush: advancing by 0 runs every pending microtask, no real wait
    answered(full());
    const pending: Array<{ path: string; release: () => void }> = [];
    const fetch = vi.fn((path: string) => new Promise<Response>((resolve) => {
      pending.push({ path, release: () => resolve(new Response(JSON.stringify(path.endsWith("7d") ? full({}, "7d") : full()), { status: 200 })) });
    }));
    vi.stubGlobal("fetch", fetch);
    render(Activity, { ontab: () => {} });
    const group = screen.getByRole("group", { name: "Window" });
    const seven = screen.getByRole("button", { name: "7 d" });
    const day = screen.getByRole("button", { name: "24 h" });
    expect(group).not.toHaveAttribute("aria-busy", "true");
    expect(day).toHaveAttribute("aria-pressed", "true");

    seven.focus();
    await fireEvent.click(seven);
    expect(group).toHaveAttribute("aria-busy", "true");
    expect(seven).toBeEnabled();
    expect(day).toBeEnabled();
    expect(seven).toHaveAttribute("aria-pressed", "true");
    expect(day).toHaveAttribute("aria-pressed", "false");

    await fireEvent.click(seven);                              // the choice already made: nothing to read
    expect(fetch).toHaveBeenCalledTimes(1);
    await fireEvent.click(day);                                // another choice meanwhile: the last one wins
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/activity?window=7d", "/api/activity?window=24h"]);
    expect(day).toHaveAttribute("aria-pressed", "true");
    expect(group).toHaveAttribute("aria-busy", "true");

    pending[0].release();                                      // the 7 d answer lands late: dropped, and the read of 24 h still runs
    await vi.advanceTimersByTimeAsync(0);                      // the read has taken the answer and dropped it
    expect(app.activity?.window).toBe("24h");
    expect(group).toHaveAttribute("aria-busy", "true");
    pending[1].release();
    await vi.advanceTimersByTimeAsync(0);
    expect(group).not.toHaveAttribute("aria-busy", "true");
    expect(document.activeElement).toBe(seven);                // the focus stayed on the control it was on
    expect(screen.getByRole("button", { name: "7 d" })).toBe(seven);
  });
});
