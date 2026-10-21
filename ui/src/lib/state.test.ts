import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { accountsFixture, activityFixture, answers, callsTo, hangs, modeCore, routes, statusFixture } from "../test-helpers";
import { ApiError, token } from "./api";
import { app, canCopy, chooseMode, choosePosition, chooseWindow, copyKey, errorText, holdPosition, loadActivity, loadStatus, refreshAccounts, saveSettings, sendCallback, startPolling, stopPolling } from "./state.svelte";

function visibility(state: "visible" | "hidden") {
  vi.spyOn(document, "visibilityState", "get").mockReturnValue(state);
}

describe("the state module", () => {
  beforeEach(() => {
    token.set(null);
    Object.assign(app, { status: null, endpoint: null, accounts: null, schema: null, settings: null, error: null, busy: false, update: null, activity: null, activityWindow: "24h", activityBusy: false, modeWrite: { flight: null, waiting: null, held: null, failure: null } });
  });
  afterEach(() => { delete window.lokki; stopPolling(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("follows a PUT with a read of the settings and then of the status", async () => {
    const fetch = answers([200, { applied: ["routing"], restart_required: [], comments_dropped: false }],
      [200, { writable: true }], [200, statusFixture()]);
    const result = await saveSettings({ routing: "relay" });
    expect(result.applied).toEqual(["routing"]);
    expect(fetch.mock.calls.map((c) => [c[1].method, c[0]])).toEqual(
      [["PUT", "/api/settings"], ["GET", "/api/settings"], ["GET", "/api/status"]]);
    expect(app.status?.revision).toBe("abc1234");
  });

  it("chooses a mode with one write and its reads", async () => {
    const fetch = answers([200, { applied: ["routing"], restart_required: [], comments_dropped: false }],
      [200, { writable: true }], [200, statusFixture()]);
    await chooseMode("auto");
    expect(fetch.mock.calls.map((c) => [c[1].method, c[0], c[1].body])).toEqual([
      ["PUT", "/api/settings", '{"routing":"auto"}'],
      ["GET", "/api/settings", undefined], ["GET", "/api/status", undefined]]);
  });

  it("chooses a position with one write and its reads", async () => {
    const fetch = answers([200, { applied: ["routing", "low_quota.threshold_percent"], restart_required: [], comments_dropped: false }],
      [200, { writable: true }], [200, statusFixture()]);
    app.status = statusFixture();
    await choosePosition(45);
    expect(fetch.mock.calls.map((c) => [c[1].method, c[0], c[1].body])).toEqual([
      ["PUT", "/api/settings", '{"routing":"auto","low_quota":{"threshold_percent":45}}'],
      ["GET", "/api/settings", undefined], ["GET", "/api/status", undefined]]);
    expect(app.modeWrite.flight).toBeNull();
    answers([409, { error: { message: "the settings file names no client token; the reload would refuse it, so nothing is written" } }]);
    await expect(choosePosition(50)).rejects.toMatchObject({ kind: "refused", status: 409 });
  });

  // One mode write at a time, in the window: decision 21, spec 3.7.
  const puts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter((c) => c[1]?.method === "PUT").map((c) => c[1].body as string);
  const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); await new Promise((r) => setTimeout(r, 0)); };

  it("sends a choice made while a write is in flight after it returns", async () => {
    for (const order of ["position first", "mode first"] as const) {
      app.modeWrite = { flight: null, waiting: null, held: null, failure: null };
      const core = modeCore("auto", 30);
      core.ctl.hold = true;
      const first = order === "position first" ? choosePosition(45) : chooseMode("relay");
      const second = order === "position first" ? chooseMode("relay") : choosePosition(45);
      await settle();
      expect(puts(core.fetch), order).toHaveLength(1);                       // the second waits
      expect(app.modeWrite.waiting, order).not.toBeNull();
      core.release();
      await first;
      await settle();
      expect(puts(core.fetch), order).toHaveLength(2);
      core.release();
      await second;
      expect(puts(core.fetch), order).toEqual(order === "position first"
        ? ['{"routing":"auto","low_quota":{"threshold_percent":45}}', '{"routing":"relay"}']
        : ['{"routing":"relay"}', '{"routing":"auto","low_quota":{"threshold_percent":45}}']);
      // The second is sent after the first write's reads, never between them.
      const paths = core.fetch.mock.calls.map((c) => `${c[1]?.method ?? "GET"} ${c[0]}`);
      expect(paths, order).toEqual(["PUT /api/settings", "GET /api/settings", "GET /api/status", "PUT /api/settings", "GET /api/settings", "GET /api/status"]);
      expect(app.modeWrite, order).toMatchObject({ flight: null, waiting: null });
    }
  });

  it("keeps only the latest of several waiting choices", async () => {
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    const direct = chooseMode("direct");
    await settle();
    const auto = chooseMode("auto");
    const relay = chooseMode("relay");
    await settle();
    expect(app.modeWrite.waiting).toEqual({ mode: "relay" });
    core.release();
    await direct;
    await settle();
    core.release();
    await Promise.all([auto, relay]);
    expect(puts(core.fetch)).toEqual(['{"routing":"direct"}', '{"routing":"relay"}']);     // Auto never sent
    expect(app.status?.routing).toBe("relay");
  });

  it("the slider's own timer firing with a held position sends one PUT", async () => {
    vi.useFakeTimers();
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    holdPosition(31);
    holdPosition(32);                       // two ArrowRight from 30
    expect(app.modeWrite.held).toBe(32);
    await vi.advanceTimersByTimeAsync(499);
    expect(puts(core.fetch)).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    // The timer's choice is the held position itself: it is in flight, and nothing waits behind it.
    expect(app.modeWrite).toMatchObject({ flight: { position: 32 }, waiting: null, held: null });
    core.release();
    await vi.advanceTimersByTimeAsync(600);
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":32}}']);
    expect(app.modeWrite.held).toBeNull();
    expect(app.status?.low_quota.threshold_percent).toBe(32);
  });

  it("sends a waiting choice after a write that fails", async () => {
    for (const failure of [[409, { error: { message: "refused" } }], "network"] as const) {
      app.modeWrite = { flight: null, waiting: null, held: null, failure: null };
      const core = modeCore("auto", 30);
      core.ctl.hold = true;
      core.ctl.refuse = [failure];
      const first = choosePosition(45);
      first.catch(() => {});
      const second = chooseMode("relay");
      await settle();
      core.release();
      await expect(first).rejects.toBeInstanceOf(ApiError);
      await settle();
      core.release();
      await second;
      expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":45}}', '{"routing":"relay"}']);
      expect(app.status?.routing).toBe("relay");
      expect(app.modeWrite.failure).toBeNull();             // the waiting write's success clears the line
      app.error = null;
    }
  });

  it("sends a waiting choice after a write whose read back failed, whatever the stale status says", async () => {
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    core.ctl.statusFails = 1;
    const first = choosePosition(45);
    const second = choosePosition(30);           // the latest choice, equal to the status the window still holds
    await settle();
    core.release();
    await first;
    await settle();
    core.release();
    await second;
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":45}}', '{"routing":"auto","low_quota":{"threshold_percent":30}}']);
    expect(core.state.threshold).toBe(30);
    app.error = null;
  });

  // Decision 21: a waiting choice is compared with what the core reports when it is sent, not when it is chosen.
  it("compares a waiting choice with the core when it is sent, not when it is chosen", async () => {
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    const relay = chooseMode("relay");
    await settle();
    const back = choosePosition(30);             // equal to the status when chosen, not once Relay has landed
    await settle();
    core.release();
    await relay;
    await settle();
    core.release();
    await back;
    expect(puts(core.fetch)).toEqual(['{"routing":"relay"}', '{"routing":"auto","low_quota":{"threshold_percent":30}}']);
    expect(core.state).toEqual({ routing: "auto", threshold: 30 });
  });

  it("shows the shell a 401 from a save and hands a 409 to the caller only", async () => {
    answers([401, { error: { message: "missing token" } }]);
    await expect(saveSettings({ routing: "relay" })).rejects.toMatchObject({ kind: "auth" });
    expect(app.error).toMatchObject({ kind: "auth" });
    app.error = null;
    answers([409, { error: { message: "an existing cliproxy.url decides the upstream" }, key: "cliproxy.port" }]);
    await expect(saveSettings({ cliproxy: { port: 1 } })).rejects.toMatchObject({ kind: "refused", key: "cliproxy.port" });
    expect(app.error).toBeNull();
  });

  it("reports whether the key was copied, and passes on the clipboard's refusal", async () => {
    const writeText = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("Write permission denied."));
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const key = { urls: [], key_masked: "…abcd", key: "sk-full" };
    answers([200, key], [200, key]);
    expect(await copyKey()).toBe(true);
    await expect(copyKey()).rejects.toThrow("Write permission denied.");
    expect(JSON.stringify(app)).not.toContain("sk-full");
  });

  it("copies the revealed key to the clipboard and never holds it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const fetch = answers([200, { urls: [], key_masked: "…abcd", key: "sk-full" }]);
    expect(canCopy()).toBe(true);
    await copyKey();
    expect(fetch.mock.calls[0][0]).toBe("/api/endpoint?reveal=1");
    expect(writeText).toHaveBeenCalledWith("sk-full");
    expect(app.endpoint?.key).toBeUndefined();
    expect(JSON.stringify(app)).not.toContain("sk-full");
  });

  it("cannot copy without a clipboard", () => {
    vi.stubGlobal("navigator", {});
    expect(canCopy()).toBe(false);
  });

  it("polls the status at once and every 10 s, but not while the page is hidden", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/endpoint": { urls: [], key_masked: null }, "/api/accounts": accountsFixture() });
    startPolling("home");
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/status")).toBe(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(callsTo(fetch, "/api/status")).toBe(2);
    visibility("hidden");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/status")).toBe(2);
    visibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/status")).toBe(3);
  });

  it.each(["home", "settings"] as const)("reads the update state with every status poll on the %s tab when the shell provides the hook", async (tab) => {
    vi.useFakeTimers();
    visibility("visible");
    routes({ "/api/status": statusFixture(), "/api/endpoint": { urls: [], key_masked: null }, "/api/accounts": accountsFixture() });
    startPolling(tab);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(app.update).toBeNull();         // a plain browser: no hook, no banner
    stopPolling();
    const available = { state: "available", current_version: "0.1.0", version: "0.2.0", notes_url: null, can_install: true, reason: null, error: null } as const;
    const updateStatus = vi.fn(async () => available);
    window.lokki = { updateStatus };
    startPolling(tab);
    await vi.advanceTimersByTimeAsync(0);
    expect(updateStatus).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(updateStatus).toHaveBeenCalledTimes(2);
    expect(app.update).toEqual(available);
  });

  it("loads the endpoint once and the accounts with every status on Home", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/endpoint": { urls: ["http://127.0.0.1:8381/v1"], key_masked: "…abcd" }, "/api/accounts": accountsFixture() });
    startPolling("home");
    await vi.advanceTimersByTimeAsync(20_000);
    expect(callsTo(fetch, "/api/status")).toBe(3);
    expect(callsTo(fetch, "/api/accounts")).toBe(3);
    expect(callsTo(fetch, "/api/endpoint")).toBe(1);
    expect(app.endpoint?.key_masked).toBe("…abcd");
    expect(app.accounts?.read_at).toBe(1_800_000_000);
  });

  it("polls the 24 h activity every 30 s on Home, not on other tabs, never two at once", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const table = { "/api/status": statusFixture(), "/api/endpoint": { urls: [], key_masked: null }, "/api/accounts": accountsFixture(), "/api/activity?window=24h": activityFixture() };
    const fetch = routes(table);
    startPolling("home");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(3);        // at once, at 30 s, at 60 s
    expect(app.activity?.window).toBe("24h");
    stopPolling();

    const other = routes(table);
    startPolling("accounts");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(callsTo(other, "/api/activity?window=24h")).toBe(0);
    stopPolling();

    const slow = hangs(false);
    startPolling("home");
    await vi.advanceTimersByTimeAsync(120_000);
    expect(callsTo(slow, "/api/activity?window=24h")).toBe(1);
  });

  it("polls the chosen window's activity every 30 s on Activity, and reads a new choice at once", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/activity?window=24h": activityFixture(), "/api/activity?window=7d": activityFixture({}, {}, "7d") });
    startPolling("activity");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(2);
    expect(callsTo(fetch, "/api/status")).toBe(4);                      // at once, then every 10 s
    await chooseWindow("7d");
    expect(callsTo(fetch, "/api/activity?window=7d")).toBe(1);
    expect(app.activity?.window).toBe("7d");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/activity?window=7d")).toBe(2);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(2);         // the old window is no longer read
  });

  it("reads the 24 h activity on Home when 7 d was chosen on Activity", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/endpoint": { urls: [], key_masked: null }, "/api/accounts": accountsFixture(), "/api/activity?window=24h": activityFixture(), "/api/activity?window=7d": activityFixture({}, {}, "7d") });
    app.activityWindow = "7d";
    startPolling("home");
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(1);
    expect(callsTo(fetch, "/api/activity?window=7d")).toBe(0);
    expect(app.activity?.window).toBe("24h");          // stored: Home wants 24 h whatever Activity chose
  });

  it("drops an activity answer for a window no longer chosen", async () => {
    vi.useFakeTimers();
    visibility("hidden");
    const held: Array<() => void> = [];
    vi.stubGlobal("fetch", vi.fn((path: string) => new Promise<Response>((done) => {
      const body = path.endsWith("7d") ? activityFixture({}, {}, "7d") : activityFixture();
      held.push(() => done(new Response(JSON.stringify(body), { status: 200 })));
    })));
    startPolling("activity");
    const first = loadActivity("24h");              // a 24 h read held pending
    const chosen = chooseWindow("7d");
    held[1]();
    await chosen;
    expect(app.activity?.window).toBe("7d");
    held[0]();
    await first;
    expect(app.activity?.window).toBe("7d");        // the late 24 h answer is not stored
  });

  it("reads the 24 h activity once when a hidden Home becomes visible", async () => {
    vi.useFakeTimers();
    const state = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/endpoint": { urls: [], key_masked: null }, "/api/accounts": accountsFixture(), "/api/activity?window=24h": activityFixture() });
    startPolling("home");
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(1);
    state.mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(1);        // hidden: nothing read
    state.mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(2);
  });

  it("stores an activity answer only for the window the open screen wants", async () => {
    vi.useFakeTimers();
    visibility("hidden");                       // the screens are entered, but no poll of their own lands in the test
    const late = routes({ "/api/activity?window=24h": activityFixture(), "/api/activity?window=7d": activityFixture({}, {}, "7d"), "/api/status": statusFixture() });
    startPolling("home");                       // Home wants 24h
    await loadActivity("7d");                   // an answer for a window nobody asked for
    expect(app.activity).toBeNull();
    await loadActivity("24h");
    expect(app.activity?.window).toBe("24h");
    app.activity = null;

    let release: () => void = () => {};
    late.mockImplementation((path: string) => new Promise<Response>((done) => {
      release = () => done(new Response(JSON.stringify(activityFixture()), { status: 200 }));
      void path;
    }));
    const pending = loadActivity("24h");
    startPolling("settings");                   // the user leaves Home before the answer lands
    release();
    await pending;
    expect(app.activity).toBeNull();
  });

  // Settings counts the watched accounts from one read on entry; only the status is polled there.
  it("reads the accounts once on entering Settings, and polls only the status there", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/accounts": accountsFixture() });
    startPolling("settings");
    await vi.advanceTimersByTimeAsync(40_000);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/api/status", "/api/accounts", "/api/status", "/api/status", "/api/status", "/api/status"]);
    expect(app.accounts).not.toBeNull();
  });

  it("skips a poll tick while the previous one has not answered", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = hangs(false);
    startPolling("home");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(callsTo(fetch, "/api/status")).toBe(1);
    expect(callsTo(fetch, "/api/accounts")).toBe(1);
  });

  it("polls again once the slow tick has answered", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = hangs();                 // each call is aborted by the 20 s timeout
    startPolling("settings");
    await vi.advanceTimersByTimeAsync(19_999);
    expect(callsTo(fetch, "/api/status")).toBe(1);
    await vi.advanceTimersByTimeAsync(10_001);
    expect(callsTo(fetch, "/api/status")).toBe(2);
  });

  it("polls the accounts every 30 s on the Accounts tab only", async () => {
    vi.useFakeTimers();
    visibility("visible");
    const fetch = routes({ "/api/status": statusFixture(), "/api/accounts": accountsFixture() });
    startPolling("accounts");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/accounts")).toBe(2);   // at once, and after 30 s
    stopPolling();
    const before = fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch.mock.calls).toHaveLength(before);
  });

  it("clears busy when a refresh gets no answer", async () => {
    vi.useFakeTimers();
    hangs();
    const refreshing = refreshAccounts();
    expect(app.busy).toBe(true);
    await vi.advanceTimersByTimeAsync(20_000);
    await refreshing;
    expect(app.busy).toBe(false);
    expect(app.error).toMatchObject({ kind: "network", message: "the core did not answer in time" });
  });

  it("keeps an authorization failure for the shell to show", async () => {
    answers([401, { error: { message: "missing token" } }]);
    await loadStatus();
    expect(app.error).toMatchObject({ kind: "auth", message: "missing token" });
  });

  it("words any thrown value as one line of text", () => {
    expect(errorText(new Error("boom"))).toBe("boom");
    expect(errorText(new ApiError(409, "an existing cliproxy.url decides the upstream", "refused", "cliproxy.port")))
      .toBe("an existing cliproxy.url decides the upstream");
    expect(errorText("plain")).toBe("plain");
  });
  it("sends the pasted address for the pending login", async () => {
    const address = "http://localhost:1455/auth/callback?code=zz-sentinel&state=st_abc12345";
    app.oauth = { state: "st_abc12345", url: "https://auth.example/x", status: "pending", message: "" };
    const fetch = answers([200, { handed_on: true }]);
    await sendCallback(address);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe("/api/accounts/oauth/st_abc12345");            // the state is the path, the address only the body
    expect(fetch.mock.calls[0][1].method).toBe("POST");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ url: address });
    expect(JSON.stringify(app)).not.toContain("sentinel");
    expect(app.oauth).toMatchObject({ status: "pending" });

    app.oauth = null;                                                                  // no pending login: nothing to send
    await sendCallback(address);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    { status: 409, body: { error: { message: "this login expired; start again" } }, kind: "refused", shell: null },
    { status: 502, body: { error: { message: "CLIProxyAPI did not take this login" } }, kind: "other", shell: null },
    { status: 401, body: { error: { message: "the operator token was rejected" } }, kind: "auth", shell: "auth" },
  ])("throws the core's refusal of the pasted address as it is: $status", async ({ status, body, kind, shell }) => {
    app.oauth = { state: "st_abc12345", url: "https://auth.example/x", status: "pending", message: "" };
    app.error = null;
    answers([status, body]);
    await expect(sendCallback("http://localhost:1455/auth/callback?code=zz-sentinel&state=st_abc12345"))
      .rejects.toMatchObject({ kind, message: body.error.message });
    expect((app.error as ApiError | null)?.kind ?? null).toBe(shell);   // a token refusal also brings up the prompt
    expect((app.error as ApiError | null)?.message ?? "").not.toContain("sentinel");                        // JSON.stringify skips a message
    expect(JSON.stringify(app)).not.toContain("sentinel");
    expect(app.oauth).toMatchObject({ status: "pending" });                            // the login itself is the polling's to end
  });
});
