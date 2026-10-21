import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { accountsFixture, answers, settingsFixture, statusFixture } from "../test-helpers";
import { ApiError, type Account, type Accounts as AccountsBody } from "../lib/api";
import { clock } from "../lib/format";
import { NO_SECRET, UNWATCHED_MODES } from "../lib/mode";
import { app, cancelOauth } from "../lib/state.svelte";
import Accounts from "./Accounts.svelte";

function account(over: Partial<Account>): Account {
  return {
    auth_index: "a1", label: "first", email_masked: "o***e", enabled: true, used_percent: 80, remaining_percent: 20,
    reset_at: 1_800_000_000, state: "ok", error: null, in_scope: true, ...over,
  };
}

const accountsBody = (rows: Account[], over: Partial<AccountsBody> = {}) => accountsFixture({ accounts: rows, ...over });
const quota = (over: object) => statusFixture({ low_quota: { ...statusFixture().low_quota, ...over } });
const ok = { applied: ["low_quota.accounts"], restart_required: [], comments_dropped: false };

const row = (key: string) => {
  const found = document.querySelector<HTMLElement>(`[data-key="${key}"]`);
  if (!found) throw new Error(`no card ${key}`);
  return found;
};

const START = { state: "st_abc12345", url: "https://auth.example/authorize?state=st_abc12345" };
const poll = (status: "pending" | "ok" | "error", error: string | null = null): [number, unknown] => [200, { status, error }];
const refused = (status: number, message: string): [number, unknown] => [status, { error: { message } }];

const advance = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms); await tick(); };
const addAccount = async () => {
  await fireEvent.click(screen.getByRole("button", { name: "Add a Codex account" }));
  await advance(0);
};
const requests = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.map((c) => `${c[1].method} ${c[0]}`);

describe("Accounts", () => {
  beforeEach(() => {
    Object.assign(app, { status: statusFixture(), endpoint: null, accounts: null, schema: null, settings: null, error: null, busy: false, oauth: null });
  });
  afterEach(() => { cleanup(); cancelOauth(); delete window.lokki; vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("shows each account as a card keyed on auth_index, not the label", () => {
    app.accounts = accountsBody([
      account({ auth_index: "a1", label: "first", email_masked: "o***e" }),
      account({ auth_index: "b2", label: "second", email_masked: "t***o", remaining_percent: null, reset_at: null,
        state: "unknown", error: "HTTP 500 on the usage route" }),
      account({ auth_index: "c3", label: "third", email_masked: "x***y", remaining_percent: 10, state: "low" }),
    ]);
    render(Accounts);
    expect(row("a1")).toHaveTextContent("o***e");
    expect(within(row("a1")).getByRole("meter")).toHaveAttribute("aria-valuenow", "20");
    expect(row("a1")).toHaveTextContent("20 %");
    expect(row("a1")).toHaveTextContent("ok");
    expect(row("c3")).toHaveTextContent("low");
    expect(row("b2")).toHaveTextContent("t***o");
    expect(row("b2")).toHaveTextContent("no reading");
    expect(row("b2")).toHaveTextContent("unknown");
    expect(row("b2")).toHaveTextContent("HTTP 500 on the usage route");
    expect(document.querySelector('[data-key="first"]')).toBeNull();
  });

  // Review Focus 3
  it("writes the scope as low_quota.accounts, with an empty list for every enabled account", async () => {
    const both = [account({ auth_index: "a1", label: "first" }), account({ auth_index: "b2", label: "second", email_masked: "t***o" })];
    app.accounts = accountsBody(both);
    const fetch = answers(
      [200, ok], [200, { writable: true }], [200, quota({ accounts: ["b2"] })],
      [200, accountsBody([{ ...both[0], in_scope: false }, both[1]])],
      [200, ok], [200, { writable: true }], [200, statusFixture()], [200, accountsBody(both)]);
    render(Accounts);

    const every = screen.getByRole("radio", { name: "Every enabled account" });
    expect(every).toBeChecked();
    expect(screen.getByText("the switches apply once you pick accounts")).toBeInTheDocument();
    for (const watched of screen.getAllByRole("switch", { name: /^Watched/ })) {
      expect(watched).toBeDisabled();
      expect(watched).toHaveAttribute("aria-checked", "true");           // under every enabled account each one is watched
    }

    await fireEvent.click(screen.getByRole("radio", { name: "Only the ones I pick" }));
    expect(screen.getByRole("switch", { name: "Watched: o***e" })).toBeEnabled();
    expect(fetch).not.toHaveBeenCalled();                                // picking the second choice writes nothing until an account is picked
    await fireEvent.click(within(row("b2")).getByRole("switch", { name: /^Watched/ }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ low_quota: { accounts: ["b2"] } });

    await fireEvent.click(screen.getByRole("radio", { name: "Every enabled account" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(8));
    expect(JSON.parse(fetch.mock.calls[4][1].body)).toEqual({ low_quota: { accounts: [] } });

    expect(requests(fetch)).toEqual([
      "PUT /api/settings", "GET /api/settings", "GET /api/status", "GET /api/accounts",
      "PUT /api/settings", "GET /api/settings", "GET /api/status", "GET /api/accounts",
    ]);
  });

  it("posts a refresh and shows the previous snapshot with the reason the read failed", async () => {
    app.accounts = accountsBody([account({ auth_index: "a1" })]);
    const stale = accountsBody([account({ auth_index: "c3", label: "third", email_masked: "x***y" })], {
      age_s: 400, poll: { last_ok_at: null, last_error: "timeout", reads: 2, running: true },
    });
    const fetch = answers([200, stale]);
    render(Accounts);
    await fireEvent.click(screen.getByRole("button", { name: "Read now" }));
    await waitFor(() => expect(row("c3")).toHaveTextContent("x***y"));
    expect(requests(fetch)).toEqual(["POST /api/accounts/refresh"]);
    expect(screen.getByText(/last read 6 min ago/)).toBeInTheDocument();
    expect(screen.getByText(/The last read failed: timeout/)).toBeInTheDocument();
    expect(document.querySelector('[data-key="a1"]')).toBeNull();
  });

  it("disables Read now and says Reading… until the read answers", async () => {
    app.accounts = accountsBody([account({})]);
    let answer: (r: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((resolve) => { answer = resolve; })));
    render(Accounts);
    await fireEvent.click(screen.getByRole("button", { name: "Read now" }));
    expect(await screen.findByRole("button", { name: "Reading…" })).toBeDisabled();
    answer(new Response(JSON.stringify(accountsBody([account({})])), { status: 200 }));
    expect(await screen.findByRole("button", { name: "Read now" })).toBeEnabled();
  });

  it("says no account was read yet", () => {
    app.accounts = accountsBody([], { read_at: null, age_s: null });
    render(Accounts);
    expect(screen.getByText("No account read yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read now" })).toBeEnabled();
  });

  it("says there is no account yet and offers to add one, once the core read and found none", () => {
    app.accounts = accountsBody([]);
    for (const routing of ["direct", "auto", "relay"] as const) {
      app.status = statusFixture({ routing });
      const view = render(Accounts);
      expect(screen.getByText("No Codex account yet")).toBeInTheDocument();
      // True in every mode: the quota is read whatever the mode, and only Auto decides by it.
      expect(screen.getByText("Add the account your coding client uses; lokki reads its quota in every mode.")).toBeInTheDocument();
      expect(screen.getAllByRole("button", { name: "Add a Codex account" })).toHaveLength(1);
      view.unmount();
    }
  });

  it("says why there is no account when the accounts read itself failed", () => {
    app.accounts = null;
    app.failed = { schema: null, settings: null, accounts: new ApiError(0, "the core is not answering", "network") };
    render(Accounts);
    expect(screen.getByText("The accounts could not be read: the core is not answering")).toBeInTheDocument();
    expect(screen.queryByText("No account read yet")).toBeNull();
    app.failed = { schema: null, settings: null, accounts: null };
  });

  it("says lokki does not read the quota without a management secret file, with no banner", () => {
    const poll = { last_ok_at: null, last_error: "no management secret file is set", reads: 0, running: true };
    app.settings = settingsFixture({ cliproxy: { ...settingsFixture().cliproxy, management_secret_file: "" } });
    app.status = statusFixture({ poll, decision: { relay_on: false, reason: "the quota cannot be read" } });
    app.accounts = accountsBody([], { read_at: null, age_s: null, poll });
    render(Accounts);
    expect(screen.getByText(NO_SECRET)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText("No account read yet")).toBeNull();
  });

  it("shows the cards and Read now in every mode", () => {
    app.accounts = accountsBody([account({ auth_index: "a1" }), account({ auth_index: "b2", email_masked: "t***o" })]);
    for (const routing of ["direct", "auto", "relay"] as const) {
      app.status = statusFixture({ routing });
      const view = render(Accounts);
      expect(row("a1")).toBeInTheDocument();
      expect(row("b2")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Read now" })).toBeEnabled();
      expect(screen.getByText(/^last read /)).toBeInTheDocument();
      view.unmount();
    }
  });

  it("offers the scope and the switches only in Auto, and in Relay and Direct says Auto watches accounts", () => {
    app.accounts = accountsBody([account({ auth_index: "a1" }), account({ auth_index: "b2", email_masked: "t***o", in_scope: false })]);
    for (const routing of ["auto", "relay", "direct"] as const) {
      app.status = quota({ accounts: ["a1", "gone"] });
      app.status = { ...app.status, routing };
      const view = render(Accounts);
      const auto = routing === "auto";
      expect(screen.queryByRole("radio", { name: "Every enabled account" }) !== null, routing).toBe(auto);
      expect(screen.queryAllByRole("switch"), routing).toHaveLength(auto ? 2 : 0);
      expect(screen.queryByText("Watched but not found:") !== null, routing).toBe(auto);
      expect(screen.queryByText(UNWATCHED_MODES) !== null, routing).toBe(!auto);
      expect(document.querySelectorAll("[data-tick]"), routing).toHaveLength(auto ? 2 : 0);    // on every card in Auto, watched or not
      expect(screen.queryByText("not watched") !== null, routing).toBe(auto);
      view.unmount();
    }
  });

  it("shows no low or ok chip in Relay and Direct, and keeps unknown with its error", () => {
    app.accounts = accountsBody([
      account({ auth_index: "a1", state: "low", remaining_percent: 10 }),
      account({ auth_index: "b2", email_masked: "t***o", state: "ok" }),
      account({ auth_index: "c3", email_masked: "x***y", state: "unknown", remaining_percent: null, reset_at: null, error: "HTTP 500 on the usage route" }),
    ]);
    for (const routing of ["relay", "direct"] as const) {
      app.status = statusFixture({ routing });
      const view = render(Accounts);
      expect(row("a1"), routing).not.toHaveTextContent(/\blow\b/);
      expect(row("a1"), routing).toHaveTextContent("10 %");
      expect(row("b2"), routing).not.toHaveTextContent(/\bok\b/);
      expect(row("c3"), routing).toHaveTextContent("unknown");
      expect(row("c3"), routing).toHaveTextContent("The last read failed: HTTP 500 on the usage route");
      view.unmount();
    }
    app.status = statusFixture();
    render(Accounts);
    expect(row("a1")).toHaveTextContent(/\blow\b/);
    expect(row("b2")).toHaveTextContent(/\bok\b/);
  });

  it("shows not watched for an account out of scope, and greys a disabled one", () => {
    app.accounts = accountsBody([
      account({ auth_index: "a1" }),
      account({ auth_index: "b2", email_masked: "t***o", in_scope: false, state: "unknown", enabled: false }),
    ]);
    render(Accounts);
    expect(row("b2")).toHaveTextContent("not watched");
    expect(row("b2")).not.toHaveTextContent("unknown");
    expect(row("b2")).toHaveAttribute("aria-disabled", "true");
    expect(row("a1")).not.toHaveAttribute("aria-disabled");
    expect(row("a1")).not.toHaveTextContent("not watched");
  });

  it("says when the window resets, and that it has reset once reset_at has passed", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(1_800_000_000_000);
    const inMin = (m: number) => 1_800_000_000 + m * 60 + 10;
    app.accounts = accountsBody([
      account({ auth_index: "a1", reset_at: inMin(72), remaining_percent: 20, state: "low" }),
      account({ auth_index: "b2", reset_at: 1_800_000_000 - 60, remaining_percent: 20, state: "ok" }),
    ]);
    render(Accounts);
    expect(row("a1")).toHaveTextContent(`resets in 1 h 12 min · ${clock(inMin(72))}`);
    expect(row("a1")).not.toHaveTextContent("window reset");
    expect(row("b2")).toHaveTextContent("100 %");
    expect(row("b2")).toHaveTextContent("window reset");
    expect(row("b2")).toHaveTextContent("reset; the next read shows the new window");
    expect(row("b2")).not.toHaveTextContent("resets in");
  });

  // A core whose PUT answers when `release` is called; every read answers at once.
  function gatedWrite(after: { status: object; accounts: object }) {
    let release: () => void = () => {};
    const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
    const fetch = vi.fn((path: string, init: RequestInit) => {
      if (init.method === "PUT") return new Promise<Response>((resolve) => { release = () => resolve(new Response(JSON.stringify(ok), { status: 200 })); });
      return json(path === "/api/settings" ? { writable: true } : path === "/api/status" ? after.status : after.accounts);
    });
    vi.stubGlobal("fetch", fetch);
    return { fetch, release: () => release() };
  }
  const puts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter((c) => c[1].method === "PUT").length;

  it("keeps a Watched switch and the scope choice enabled while a write runs, and the focus on the switch after it", async () => {
    const rows = [account({ auth_index: "a1" }), account({ auth_index: "b2", email_masked: "t***o" })];
    app.status = quota({ accounts: ["a1"] });
    app.accounts = accountsBody(rows);
    const { fetch, release } = gatedWrite({ status: quota({ accounts: ["a1", "b2"] }), accounts: accountsBody(rows) });
    render(Accounts);
    const watched = screen.getByRole("switch", { name: "Watched: t***o" });
    watched.focus();
    await fireEvent.click(watched);
    expect(watched).toHaveAttribute("aria-busy", "true");
    expect(watched).toBeEnabled();
    expect(screen.getByRole("radio", { name: "Every enabled account" })).toBeEnabled();
    await fireEvent.click(screen.getByRole("radio", { name: "Every enabled account" }));
    await fireEvent.click(watched);
    expect(puts(fetch)).toBe(1);
    release();
    await waitFor(() => expect(watched).toHaveAttribute("aria-checked", "true"));
    expect(document.activeElement).toBe(watched);
  });

  it("moves the countdown and shows a passed reset as the clock advances, with no new read", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_800_000_000_000);
    app.accounts = accountsBody([
      account({ auth_index: "a1", reset_at: 1_800_000_000 + 600 + 10, state: "low" }),
      account({ auth_index: "b2", reset_at: 1_800_000_000 + 3 * 3600, state: "ok" }),
    ]);
    render(Accounts);
    expect(row("a1")).toHaveTextContent("resets in 10 min");
    expect(row("b2")).toHaveTextContent("resets in 3 h 00 min");
    await advance(300_000);
    expect(row("a1")).toHaveTextContent("resets in 5 min");
    expect(row("a1")).not.toHaveTextContent("window reset");
    await advance(400_000);
    expect(row("a1")).toHaveTextContent("100 %");
    expect(row("a1")).toHaveTextContent("window reset");
    expect(row("a1")).toHaveTextContent("reset; the next read shows the new window");
    expect(row("b2")).toHaveTextContent("resets in 2 h 48 min");
  });

  it("follows the clock at once when a new accounts answer lands, without waiting for the next tick", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });                      // the 10 s tick never fires: only a read can move the line
    vi.setSystemTime(1_800_000_000_000);
    const rows = () => accountsBody([account({ auth_index: "a1", reset_at: 1_800_000_000 + 610, state: "low" })]);
    app.accounts = rows();
    render(Accounts);
    expect(row("a1")).toHaveTextContent("resets in 10 min");
    vi.setSystemTime(1_800_000_000_000 + 700_000);
    expect(row("a1")).toHaveTextContent("resets in 10 min");
    app.accounts = rows();
    await tick();
    expect(row("a1")).toHaveTextContent("window reset");
  });

  it("says the reset time is not read when there is none, or when it has passed on an account the core does not read", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(1_800_000_000_000);
    app.accounts = accountsBody([
      account({ auth_index: "a1", reset_at: null, remaining_percent: null, state: "unknown", error: "HTTP 401" }),
      account({ auth_index: "b2", reset_at: 1_800_000_000 - 3600, state: "unknown", error: "HTTP 401" }),
    ]);
    render(Accounts);
    expect(row("a1")).toHaveTextContent("reset time not read yet");
    expect(row("b2")).toHaveTextContent("reset time not read yet");
    expect(row("b2")).not.toHaveTextContent("resets in");
    expect(row("b2")).not.toHaveTextContent("window reset");
  });

  it("says every enabled account is still watched while Only the ones I pick has nothing picked", async () => {
    app.accounts = accountsBody([account({})]);
    render(Accounts);
    await fireEvent.click(screen.getByRole("radio", { name: "Only the ones I pick" }));
    expect(screen.getByText("nothing is picked yet, so every enabled account is still watched")).toBeInTheDocument();
    expect(screen.queryByText("the switches apply once you pick accounts")).toBeNull();
  });

  it("says loading until the first status arrives", () => {
    app.status = null;
    render(Accounts);
    expect(screen.getByText("loading…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Read now" })).toBeDisabled();
  });

  it("lists a scope entry for an account that is gone and removes it with the same write and reads", async () => {
    const rows = [account({ auth_index: "a1" }), account({ auth_index: "b2", label: "second" })];
    app.status = quota({ accounts: ["b2", "gone"] });
    app.accounts = accountsBody(rows);
    const fetch = answers([200, ok], [200, { writable: true }], [200, quota({ accounts: ["b2"] })], [200, accountsBody(rows)]);
    render(Accounts);
    expect(screen.getByText("Watched but not found:")).toBeInTheDocument();
    expect(screen.getByText("gone")).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Remove gone from the scope" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4));
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ low_quota: { accounts: ["b2"] } });
    expect(requests(fetch)).toEqual(["PUT /api/settings", "GET /api/settings", "GET /api/status", "GET /api/accounts"]);
  });

  it("lists no missing entry before the first read, when every scope entry would look missing", () => {
    app.status = quota({ accounts: ["b2"] });
    app.accounts = accountsBody([], { read_at: null, age_s: null });
    render(Accounts);
    expect(document.body.textContent).not.toContain("Watched but not found");
  });

  // The login, with its waits: 2 s polls, up to 5 min, stop on any answer but a 200.
  describe("adding a Codex account", () => {
    beforeEach(() => { vi.useFakeTimers(); });
    it.each([
      { opener: "the shell's openExternal", hook: true, rejects: false },
      { opener: "window.open without a hook", hook: false, rejects: false },
      { opener: "an openExternal that rejects", hook: true, rejects: true },
    ])("opens the login URL with $opener, polls until ok, then adds the account and refreshes the list", async ({ hook, rejects }) => {
      const openExternal = rejects ? vi.fn().mockRejectedValue(new Error("no browser to open")) : vi.fn().mockResolvedValue(undefined);
      const open = vi.spyOn(window, "open").mockReturnValue(null);
      const writeText = vi.fn().mockResolvedValue(undefined);
      vi.stubGlobal("navigator", { clipboard: { writeText } });
      if (hook) window.lokki = { openExternal };
      const fetch = answers([200, START], poll("pending"), poll("ok"), [200, accountsFixture()]);
      render(Accounts);

      await addAccount();
      if (hook) expect(openExternal).toHaveBeenCalledWith(START.url);
      else expect(open).toHaveBeenCalledWith(START.url, "_blank");
      expect(hook ? open : openExternal).not.toHaveBeenCalled();
      if (rejects) expect(screen.getByRole("alert")).toHaveTextContent("no browser to open");
      else expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.getByText(START.url)).toBeInTheDocument();
      expect(screen.getByText(START.url)).not.toHaveAttribute("title");        // the link is shown once
      expect(screen.getByRole("heading", { name: "Finish the login in a browser" })).toBeInTheDocument();
      expect(screen.getByText("lokki waits up to 5 minutes.")).toBeInTheDocument();
      expect(screen.getByText("Logging in from another device? Open this link there:")).toBeInTheDocument();
      const field = screen.getByRole("textbox", { name: /Paste that address here:/ });
      expect(field).toHaveAccessibleName(/an address that starts http:\/\/localhost:1455\/auth\/callback\./);
      expect(field).toHaveAttribute("autocomplete", "off");
      expect(field).toHaveAttribute("spellcheck", "false");
      expect(field).toHaveAttribute("inputmode", "url");
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
      await fireEvent.click(screen.getByRole("button", { name: "Copy login link" }));
      expect(writeText).toHaveBeenCalledWith(START.url);

      await advance(2_000);
      await advance(2_000);
      expect(screen.getByText("Account added")).toBeInTheDocument();
      expect(requests(fetch)).toEqual(["POST /api/accounts/oauth", `GET /api/accounts/oauth/${START.state}`, `GET /api/accounts/oauth/${START.state}`]);
      await advance(1_999);
      expect(fetch).toHaveBeenCalledTimes(3);
      await advance(1);
      expect(requests(fetch).slice(3)).toEqual(["POST /api/accounts/refresh"]);
      await advance(60_000);
      expect(fetch).toHaveBeenCalledTimes(4);
    });

    it.each([
      { when: "a refused start", list: [refused(502, "CLIProxyAPI refused the login")], shown: "CLIProxyAPI refused the login" },
      { when: "a busy callback port", list: [refused(409, "another login is in progress")], shown: "another login is in progress" },
      { when: "a refused poll", list: [[200, START], poll("pending"), refused(502, "CLIProxyAPI refused the status request")], shown: "CLIProxyAPI refused the status request" },
      { when: "a failed login", list: [[200, START], poll("error", "the code expired")], shown: "the code expired" },
      { when: "a login lokki no longer waits for", list: [[200, START], poll("error", "this login is no longer waiting; start again")], shown: "this login is no longer waiting; start again" },
      { when: "a rejected token", list: [refused(401, "the operator token was rejected")], shown: "the operator token was rejected", kind: "auth" },
    ] as Array<{ when: string; list: Array<[number, unknown]>; shown: string; kind?: string }>)("shows the error from $when and stops asking", async ({ list, shown, kind }) => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const fetch = answers(...list);
      render(Accounts);
      await addAccount();
      await advance(20_000);
      expect(screen.getByText(new RegExp(shown))).toBeInTheDocument();
      expect(screen.queryByText("Account added")).toBeNull();
      expect(fetch).toHaveBeenCalledTimes(list.length);
      expect(screen.getByRole("button", { name: "Add a Codex account" })).toBeEnabled();
      expect(app.error?.kind ?? null).toBe(kind ?? null);       // a 401 or 403 also brings up the token prompt
    });

    it("keeps polling across a remount until Cancel, then stops and allows a new flow", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const fetch = answers([200, START], poll("pending"), poll("pending"), poll("pending"));
      const first = render(Accounts);
      await addAccount();
      await advance(2_000);
      first.unmount();

      render(Accounts);
      expect(screen.getByText(START.url)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Add a Codex account" })).toBeDisabled();
      await advance(2_000);
      expect(fetch).toHaveBeenCalledTimes(3);

      await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await advance(60_000);
      expect(fetch).toHaveBeenCalledTimes(3);
      expect(screen.queryByText(START.url)).toBeNull();
      expect(screen.getByRole("button", { name: "Add a Codex account" })).toBeEnabled();
    });

    it("stops asking after CLIProxyAPI's five-minute wait for the callback", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const fetch = vi.fn((path: string) => Promise.resolve(new Response(
        JSON.stringify(path === "/api/accounts/oauth" ? START : { status: "pending", error: null }),
        { status: 200, headers: { "content-type": "application/json" } })));
      vi.stubGlobal("fetch", fetch);
      render(Accounts);
      await addAccount();
      await advance(300_000);
      expect(fetch).toHaveBeenCalledTimes(150);         // the start, then a poll every 2 s from 2 s to 298 s: 149
      expect(screen.getByText("the login timed out; start again")).toBeInTheDocument();
      await advance(60_000);
      expect(fetch).toHaveBeenCalledTimes(150);
      expect(screen.getByRole("button", { name: "Add a Codex account" })).toBeEnabled();
    });

    it("still refreshes the accounts when a new login starts before the refresh is due", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const log: string[] = [];
      let logins = 0;
      vi.stubGlobal("fetch", vi.fn((path: string, init: RequestInit) => {
        log.push(`${init.method} ${path}`);
        const body = path === "/api/accounts/oauth" ? { state: `st_login_${++logins}00`, url: `https://auth.example/${logins}` }
          : path === "/api/accounts/refresh" ? accountsFixture() : { status: "ok", error: null };
        return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
      }));
      render(Accounts);
      await addAccount();
      await advance(2_000);
      expect(screen.getByText("Account added")).toBeInTheDocument();
      await addAccount();                                 // 0 s after ok; the refresh is due at 2 s
      await advance(2_000);
      expect(log).toContain("POST /api/accounts/refresh");
    });

    // A manually answered core, to land answers after the flow they belong to has moved on.
    function held() {
      const waiting: Array<{ path: string; answer: (body: unknown) => void }> = [];
      vi.stubGlobal("fetch", vi.fn((path: string) => new Promise<Response>((resolve) => {
        waiting.push({ path, answer: (body) => resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })) });
      })));
      return async (index: number, body: unknown) => { waiting[index].answer(body); await advance(0); };
    }

    it("ignores an answer that lands after Cancel, after a replacement, or after the flow was dropped while starting", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const answer = held();
      render(Accounts);

      await addAccount();                                  // request 0: start
      await answer(0, START);
      await advance(2_000);                                // request 1: poll in flight
      await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await answer(1, { status: "ok", error: null });
      expect(app.oauth).toBeNull();
      await advance(10_000);
      expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);   // no refresh follows the late ok

      await addAccount();                                  // request 2: start
      await answer(2, { state: "st_second_12", url: "https://auth.example/second" });
      await advance(2_000);                                // request 3: poll of the second login in flight
      await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      await addAccount();                                  // request 4: start of the third
      await answer(4, { state: "st_third_123", url: "https://auth.example/third" });
      await answer(3, { status: "error", error: "stale error" });
      expect(app.oauth).toMatchObject({ state: "st_third_123", status: "pending" });
      expect(screen.queryByText("stale error")).toBeNull();
      expect(screen.getByText("https://auth.example/third")).toBeInTheDocument();

      cancelOauth();
      await addAccount();                                  // request 5: start
      cancelOauth();
      const calls = vi.mocked(fetch).mock.calls.length;
      await answer(5, START);
      await advance(10_000);
      expect(app.oauth).toBeNull();
      expect(vi.mocked(fetch)).toHaveBeenCalledTimes(calls);
    });

    // The paste field of a login finished on another device (spec 4.5). The address is a sentinel, so a
    // test can look for any part of it.
    const ADDR = "http://localhost:1455/auth/callback?code=zz-sentinel-code&state=st_abc12345";
    const SEND_PATH = `/api/accounts/oauth/${START.state}`;
    const field = () => screen.getByRole("textbox", { name: /Paste that address here:/ }) as HTMLInputElement;
    const paste = (text: string) => fireEvent.input(field(), { target: { value: text } });
    const send = () => fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));

    // A core for one login: the start, `statuses` for the polls (the last repeats), and `onSend` for each paste.
    function core(onSend: (n: number) => Promise<Response>, statuses: Array<{ status: string; error: string | null }> = [{ status: "pending", error: null }]) {
      let sends = 0;
      let polls = 0;
      const fetch = vi.fn((path: string, init: RequestInit) => {
        if (init.method === "POST" && path === "/api/accounts/oauth") return json(200, START);
        if (init.method === "POST" && path === SEND_PATH) return onSend(++sends);
        if (path === SEND_PATH) return json(200, statuses[Math.min(polls++, statuses.length - 1)]);
        return json(200, accountsFixture());
      });
      vi.stubGlobal("fetch", fetch);
      return fetch;
    }
    const posts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter((c) => c[1].method === "POST" && c[0] === SEND_PATH);

    it("sends the pasted address once, busy, and empties the field on success", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      let answer: (r: Response) => void = () => {};
      const fetch = core(() => new Promise<Response>((resolve) => { answer = resolve; }));
      render(Accounts);
      await addAccount();
      await paste(ADDR);
      await send();
      await send();                                                             // a press while it runs
      expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
      expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-busy", "true");
      expect(posts(fetch)).toHaveLength(1);
      expect(JSON.parse(posts(fetch)[0][1].body)).toEqual({ url: ADDR });       // the address is in the body only
      expect(fetch.mock.calls.every((c) => !String(c[0]).includes("sentinel") && !String(c[0]).includes("1455"))).toBe(true);

      answer(new Response(JSON.stringify({ handed_on: true }), { status: 200, headers: { "content-type": "application/json" } }));
      await advance(0);
      expect(field().value).toBe("");
      expect(screen.getByRole("button", { name: "Send" })).not.toHaveAttribute("aria-busy");
      expect(screen.getByText("Sent; finishing the login…")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).toBeNull();
      await advance(2_000);                                                     // the polling goes on
      expect(requests(fetch).filter((r) => r === `GET ${SEND_PATH}`)).toHaveLength(1);
      expect(screen.getByRole("heading", { name: "Finish the login in a browser" })).toBeInTheDocument();
    });

    it("sends on Enter once while a send runs", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      let answer: (r: Response) => void = () => {};
      const fetch = core(() => new Promise<Response>((resolve) => { answer = resolve; }));
      render(Accounts);
      await addAccount();
      await paste(ADDR);
      await fireEvent.keyDown(field(), { key: "Enter" });
      await fireEvent.keyDown(field(), { key: "Enter" });                       // a second Enter while it runs
      expect(posts(fetch)).toHaveLength(1);
      expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-busy", "true");
      answer(new Response(JSON.stringify({ handed_on: true }), { status: 200, headers: { "content-type": "application/json" } }));
      await advance(0);
      expect(field().value).toBe("");
      expect(posts(fetch)).toHaveLength(1);
    });

    // The route's answers, word for word (spec 4.4, control.py): shown as they come, under the field.
    it.each([
      { status: 400, sentence: "this is not the address the login ended on: it starts http://localhost:1455/auth/callback?" },
      { status: 409, sentence: "this login is no longer waiting; start again" },
      { status: 409, sentence: "this address is from another login; paste the address of the login started last" },
      { status: 409, sentence: "lokki already has this login's answer; wait for it to finish" },
      { status: 409, sentence: "this login expired; start again" },
      { status: 409, sentence: "CLIProxyAPI did not take this login; start again" },
      { status: 502, sentence: "CLIProxyAPI did not take this login" },
    ])("keeps the pasted address and shows the refusal under the field: $sentence", async ({ status, sentence }) => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const fetch = core(() => json(status, { error: { message: sentence } }));
      render(Accounts);
      await addAccount();
      await paste(ADDR);
      await send();
      await advance(0);
      const alert = screen.getByRole("alert");
      expect(alert).toHaveTextContent(sentence);
      expect(alert.textContent!.trim()).toBe(sentence);                         // the core's sentence, not a paraphrase
      expect(field()).toHaveAttribute("aria-invalid", "true");
      expect(field()).toHaveAccessibleDescription(sentence);
      expect(field().value).toBe(ADDR);
      expect(screen.queryByText("Sent; finishing the login…")).toBeNull();
      expect(screen.getByRole("button", { name: "Send" })).not.toHaveAttribute("aria-busy");
      await advance(2_000);                                                     // the polling goes on
      expect(requests(fetch).filter((r) => r === `GET ${SEND_PATH}`)).toHaveLength(1);

      await send();                                                             // a second Send gets the same sentence
      await advance(0);
      expect(posts(fetch)).toHaveLength(2);
      expect(screen.getByRole("alert").textContent!.trim()).toBe(sentence);
      expect(field().value).toBe(ADDR);
    });

    it.each(["", "   "])("says paste the address first and sends nothing for an empty field (%j)", async (text) => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const fetch = core(() => json(200, { handed_on: true }));
      render(Accounts);
      await addAccount();
      await paste(text);
      await send();
      await advance(0);
      expect(screen.getByRole("alert")).toHaveTextContent("paste the address first");
      expect(posts(fetch)).toHaveLength(0);
      expect(screen.queryByText("Sent; finishing the login…")).toBeNull();
    });

    it("clears the field on Cancel and when the login ends, and never puts the address in app", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const logs = (["log", "info", "warn", "error", "debug"] as const).map((m) => vi.spyOn(console, m).mockImplementation(() => {}));
      let how: "refuse" | "auth" | "ok" = "refuse";
      const answerFor = () => (how === "refuse" ? json(409, { error: { message: "this login expired; start again" } })
        : how === "auth" ? json(401, { error: { message: "the operator token was rejected" } }) : json(200, { handed_on: true }));
      const fetch = core(answerFor,
        [{ status: "pending", error: null }, { status: "pending", error: null }, { status: "ok", error: null }]);
      const inApp = () => JSON.stringify(app);
      // JSON.stringify skips an Error's message, so the messages the state keeps are looked at on their own.
      const clean = () => {
        const messages = [app.error, ...Object.values(app.failed)].map((e) => e?.message ?? "");
        for (const part of [ADDR, "sentinel", "zz-sentinel-code", "auth/callback", "localhost:1455"]) {
          expect(inApp()).not.toContain(part);
          for (const m of messages) expect(m).not.toContain(part);
        }
      };
      render(Accounts);

      await addAccount();                                                       // a refusal
      await paste(ADDR);
      await send();
      await advance(0);
      expect(field().value).toBe(ADDR);
      clean();

      how = "auth";                                                             // a rejected token, which the shell shows
      await send();
      await advance(0);
      expect(app.error).toMatchObject({ kind: "auth", message: "the operator token was rejected" });
      clean();
      app.error = null;

      await fireEvent.click(screen.getByRole("button", { name: "Cancel" }));    // Cancel
      expect(app.oauth).toBeNull();
      clean();
      await addAccount();
      expect(field().value).toBe("");
      expect(screen.queryByRole("alert")).toBeNull();

      how = "ok";                                                               // a send, then the login's end
      await paste(ADDR);
      await send();
      await advance(0);
      expect(field().value).toBe("");
      clean();
      await paste(ADDR);                                                        // typed again, then the login ends
      await advance(2_000);
      await advance(2_000);
      await advance(2_000);
      expect(screen.getByText("Account added")).toBeInTheDocument();
      clean();
      await addAccount();
      expect(field().value).toBe("");
      expect(screen.queryByText("Sent; finishing the login…")).toBeNull();

      await paste(ADDR);                                                        // a failed login ends it too
      cancelOauth();
      const failing = core(() => json(200, { handed_on: true }), [{ status: "error", error: "the code expired" }]);
      await addAccount();
      await advance(2_000);
      expect(screen.getByText("the code expired")).toBeInTheDocument();
      clean();
      for (const f of [fetch, failing]) expect(f.mock.calls.every((c) => !String(c[0]).includes("sentinel"))).toBe(true);
      for (const log of logs) expect(JSON.stringify(log.mock.calls)).not.toContain("sentinel");
    });

    it.each([
      { opener: "openExternal resolves", hook: "resolves", open: null, shown: true },
      { opener: "openExternal rejects", hook: "rejects", open: null, shown: false },
      { opener: "window.open returns a window", hook: "none", open: { opener: window }, shown: true },
      { opener: "window.open returns nothing (a blocked popup)", hook: "none", open: null, shown: false },
    ])("says the login page opened only when it did: $opener", async ({ hook, open, shown }) => {
      vi.spyOn(window, "open").mockReturnValue(open as Window | null);
      if (hook !== "none") window.lokki = { openExternal: hook === "rejects" ? vi.fn().mockRejectedValue(new Error("no browser to open")) : vi.fn().mockResolvedValue(undefined) };
      answers([200, START], poll("pending"));
      render(Accounts);
      await addAccount();
      const said = screen.queryByText("lokki opened the login page in this computer’s browser.");
      if (hook === "none") {
        expect(window.open).toHaveBeenCalledWith(START.url, "_blank");          // not "noopener": that returns null even when the tab opens
        if (open) expect((open as { opener: unknown }).opener).toBeNull();      // so the opened page is cut off by hand
      }
      if (shown) expect(said).toBeInTheDocument();
      else expect(said).toBeNull();
      expect(screen.getByText("lokki waits up to 5 minutes.")).toBeInTheDocument();
    });

    it("shows a gone login as a failure line, as the core says it", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      const gone = "CLIProxyAPI no longer holds this login: it expired, or it finished more than a minute ago; check Accounts, else start again";
      answers([200, START], poll("error", gone));
      render(Accounts);
      await addAccount();
      await advance(2_000);
      expect(screen.getByRole("alert")).toHaveTextContent(gone);
      expect(screen.getByRole("alert")).toHaveClass("fail");
      expect(screen.queryByRole("textbox")).toBeNull();
    });

    it("shows Copied for three seconds after the login link is copied", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
      answers([200, START], poll("pending"));
      render(Accounts);
      await addAccount();
      await fireEvent.click(screen.getByRole("button", { name: "Copy login link" }));
      await advance(0);
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
      await advance(3_000);
      expect(screen.getByRole("button", { name: "Copy login link" })).toBeInTheDocument();
    });

    it("says Account added and nothing more, in every mode, and dismisses the notice", async () => {
      vi.spyOn(window, "open").mockReturnValue(null);
      app.status = statusFixture({ routing: "relay" });
      answers([200, START], poll("ok"), [200, accountsBody([account({})])]);
      render(Accounts);
      await addAccount();
      await advance(2_000);
      const added = screen.getByRole("heading", { name: "Account added" });
      expect(added.parentElement!.children).toHaveLength(1);                   // no second line under it
      expect(within(added.closest(".card") as HTMLElement).getAllByRole("button").map((b) => b.getAttribute("aria-label") ?? b.textContent)).toEqual(["Dismiss"]);
      await fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
      expect(screen.queryByText("Account added")).toBeNull();
    });
  });
});
