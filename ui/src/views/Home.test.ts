import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { accountFixture, accountsFixture, activityFixture, answers, settingsFixture, statusFixture } from "../test-helpers";
import { ApiError } from "../lib/api";
import { MODES, NO_SECRET, NO_SECRET_AUTO, READ_FAILED_AUTO } from "../lib/mode";
import { app } from "../lib/state.svelte";
import TopBar from "../components/TopBar.svelte";
import Home from "./Home.svelte";

const noTab = () => {};

describe("Home", () => {
  beforeEach(() => {
    Object.assign(app, {
      status: statusFixture(), endpoint: null, accounts: null, schema: null, settings: null, error: null, busy: false, activity: null,
      modeWrite: { flight: null, waiting: null, held: null, failure: null },
    });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("shows the endpoint masked", () => {
    app.endpoint = { urls: ["http://127.0.0.1:8381/v1"], key_masked: "…abcd" };
    render(Home, { ontab: noTab });
    expect(screen.getByText("http://127.0.0.1:8381/v1")).toBeInTheDocument();
    expect(screen.getByText("…abcd")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("undefined");
  });

  it("copies the key to the clipboard and never renders it", async () => {
    app.endpoint = { urls: ["http://127.0.0.1:8381/v1"], key_masked: "…abcd" };
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const fetch = answers([200, { urls: ["http://127.0.0.1:8381/v1"], key_masked: "…abcd", key: "sk-full" }]);
    render(Home, { ontab: noTab });
    await fireEvent.click(screen.getByRole("button", { name: "Copy key" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("sk-full"));
    expect(fetch.mock.calls[0][0]).toBe("/api/endpoint?reveal=1");
    expect(document.body.innerHTML).not.toContain("sk-full");
    expect(screen.getByText("…abcd")).toBeInTheDocument();
  });

  it("confirms a copy for a few seconds, without showing the key", async () => {
    vi.useFakeTimers();
    app.endpoint = { urls: [], key_masked: "…abcd" };
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    answers([200, { urls: [], key_masked: "…abcd", key: "sk-full" }]);
    render(Home, { ontab: noTab });
    await fireEvent.click(screen.getByRole("button", { name: "Copy key" }));
    await vi.advanceTimersByTimeAsync(0);
    await tick();
    expect(screen.getByText("copied")).toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(3_000);
    await tick();
    expect(screen.queryByText("copied")).toBeNull();
    expect(document.body.innerHTML).not.toContain("sk-full");
  });

  it("says why a copy failed, without showing the key", async () => {
    app.endpoint = { urls: [], key_masked: "…abcd" };
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("Write permission denied.")) } });
    answers([200, { urls: [], key_masked: "…abcd", key: "sk-full" }]);
    render(Home, { ontab: noTab });
    await fireEvent.click(screen.getByRole("button", { name: "Copy key" }));
    expect(await screen.findByText("could not copy: Write permission denied.")).toBeInTheDocument();
    expect(screen.queryByText("copied")).toBeNull();
    expect(document.body.innerHTML).not.toContain("sk-full");
  });

  it("lists two equal addresses without failing", () => {
    app.endpoint = { urls: ["http://127.0.0.1:8381/v1", "http://127.0.0.1:8381/v1"], key_masked: null };
    render(Home, { ontab: noTab });
    expect(screen.getAllByText("http://127.0.0.1:8381/v1")).toHaveLength(2);
  });

  it("disables the copy button where the browser offers no clipboard", () => {
    app.endpoint = { urls: [], key_masked: "…abcd" };
    vi.stubGlobal("navigator", {});
    render(Home, { ontab: noTab });
    expect(screen.getByRole("button", { name: "Copy key" })).toBeDisabled();
    expect(screen.getByText(/copying needs a secure origin/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy key" })).toHaveAccessibleDescription(/copying needs a secure origin/);
  });

  it("shows runs by route and whether CLIProxyAPI answers", () => {
    app.status = statusFixture({
      runs_since_start: { relay: 3, passthrough: 1, images: 0 },
      cliproxy: { url: "http://127.0.0.1:8318", reachable: false, version: "8.0.23" },
    });
    const first = render(Home, { ontab: noTab });
    const since = screen.getByRole("region", { name: "Since start" });
    for (const [route, count] of [["All requests", "4"], ["Relay", "3"], ["Pass-through", "1"], ["Images", "0"]]) {
      expect(within(since).getByText(route, { selector: "dt" }).nextElementSibling).toHaveTextContent(count);
    }
    expect(within(since).getByText("CLIProxyAPI not reachable")).toBeInTheDocument();
    expect(screen.queryByText(/8\.0\.23/)).toBeNull();         // the versions are Settings › About's
    first.unmount();

    app.status = statusFixture({ runs_since_start: {} });
    render(Home, { ontab: noTab });
    expect(within(screen.getByRole("region", { name: "Since start" })).getByText("Relay", { selector: "dt" }).nextElementSibling).toHaveTextContent("0");
    expect(screen.getByText("CLIProxyAPI running")).toBeInTheDocument();
  });

  it("shows the quota-read banner in every mode, and in Auto says an unread account counts as not low", () => {
    const poll = { last_ok_at: null, last_error: "HTTP 401 on the credentials route", reads: 1, running: true };
    const named = settingsFixture({ cliproxy: { ...settingsFixture().cliproxy, management_secret_file: "/data/secrets/cliproxy-mgmt.secret" } });
    for (const [routing, settings] of [["auto", null], ["auto", named], ["relay", named], ["direct", named]] as const) {
      app.status = statusFixture({ routing, poll });
      app.settings = settings;                                // unread, the file counts as set
      const view = render(Home, { ontab: noTab });
      expect(screen.getByRole("alert").textContent?.trim(), routing)
        .toBe("The last quota read failed: HTTP 401 on the credentials route" + (routing === "auto" ? READ_FAILED_AUTO : ""));
      view.unmount();
    }
    app.status = statusFixture();
    render(Home, { ontab: noTab });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says lokki does not read the quota without a management secret file, with no banner", () => {
    const poll = { last_ok_at: null, last_error: "no management secret file is set", reads: 0, running: true };
    for (const routing of ["auto", "relay", "direct"] as const) {
      app.status = statusFixture({ routing, poll, decision: { relay_on: routing === "relay", reason: routing === "auto" ? "the quota cannot be read" : `routing: ${routing}` } });
      app.settings = settingsFixture({ cliproxy: { ...settingsFixture().cliproxy, management_secret_file: "" } });
      app.accounts = accountsFixture({ read_at: null, age_s: null, accounts: [] });
      const view = render(Home, { ontab: noTab });
      expect(screen.getByText(routing === "auto" ? NO_SECRET + NO_SECRET_AUTO : NO_SECRET), routing).toBeInTheDocument();
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByText(/No account read yet|Reading the quota|No quota read yet/)).toBeNull();
      view.unmount();
    }
  });

  it("says Reading the quota… before the first read, and that the last read failed when there is no snapshot", () => {
    for (const accounts of [null, accountsFixture({ read_at: null, age_s: null, accounts: [] })]) {
      app.accounts = accounts;
      const view = render(Home, { ontab: noTab });
      expect(screen.getByText("Reading the quota…")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).toBeNull();
      view.unmount();
    }
    const poll = { last_ok_at: null, last_error: "CLIProxyAPI did not answer (timeout)", reads: 1, running: true };
    app.status = statusFixture({ poll, decision: { relay_on: false, reason: "the quota cannot be read" } });
    app.accounts = accountsFixture({ read_at: null, age_s: null, accounts: [], poll });
    render(Home, { ontab: noTab });
    expect(screen.getByRole("alert")).toHaveTextContent("The last quota read failed: CLIProxyAPI did not answer (timeout)");
    expect(screen.getByText("No quota read yet: the last read failed (above)")).toBeInTheDocument();
    expect(screen.queryByText("Reading the quota…")).toBeNull();
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
  });

  it("says why the strip has nothing when the accounts read itself failed", () => {
    app.accounts = null;
    app.failed = { schema: null, settings: null, accounts: new ApiError(0, "the core is not answering", "network") };
    render(Home, { ontab: noTab });
    expect(screen.getByText("The accounts could not be read: the core is not answering")).toBeInTheDocument();
    expect(screen.queryByText("Reading the quota…")).toBeNull();
    app.failed = { schema: null, settings: null, accounts: null };
  });

  it("says the slider's words: the rule line between the ends, the mode's sentence at them", () => {
    const words = () => document.querySelector(".slider-words")?.textContent?.trim();
    app.accounts = accountsFixture({ accounts: [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2", state: "ok" })] });
    const draw = () => { const bar = render(TopBar, { size: "full" }); const home = render(Home, { ontab: noTab }); return () => { home.unmount(); bar.unmount(); }; };
    let done = draw();
    expect(words()).toBe("Cut when every watched account has less than 30 % left · every model");
    done();
    app.accounts = accountsFixture({ accounts: [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2", in_scope: false })] });
    app.status = statusFixture({ low_quota: { ...statusFixture().low_quota, rule: "any", models: ["m1", "m2"] } });
    done = draw();
    expect(words()).toBe("Cut when the watched account has less than 30 % left · 2 models");
    done();
    for (const [routing, sentence] of [["direct", MODES[0].sentence], ["relay", MODES[2].sentence]] as const) {
      app.status = statusFixture({ routing });
      done = draw();
      expect(words(), routing).toBe(sentence);
      expect(screen.queryByText(/^Cut when .+ % left/)).toBeNull();
      done();
    }
  });

  it("puts the watched accounts first with their tick and chip, and the others outlined and not watched", () => {
    app.accounts = accountsFixture({ accounts: [
      accountFixture({ auth_index: "d4", email_masked: "f***@example.invalid", remaining_percent: 64, state: "ok", in_scope: false }),
      accountFixture({ auth_index: "a1", email_masked: "o***@example.invalid", remaining_percent: 20, state: "low" }),
      accountFixture({ auth_index: "c3", email_masked: "s***@example.invalid", remaining_percent: null, state: "unknown", error: "HTTP 500 on the usage route" }),
      accountFixture({ auth_index: "b2", email_masked: "t***@example.invalid", remaining_percent: 90, state: "ok" }),
    ] });
    render(Home, { ontab: noTab });
    const cards = [...document.querySelectorAll<HTMLElement>(".gmini")];
    expect(cards.map((c) => c.querySelector(".em")?.textContent)).toEqual(["o***@example.invalid", "s***@example.invalid", "t***@example.invalid", "f***@example.invalid"]);
    const [low, unread, ok, other] = cards;
    for (const watched of [low, unread, ok]) {
      expect(watched.querySelector("[data-tick]")).toHaveStyle({ left: "30%" });
      expect(watched).not.toHaveClass("unwatched");
    }
    expect(within(low).getByText("low")).toBeInTheDocument();
    expect(within(ok).getByText("ok")).toBeInTheDocument();
    expect(within(unread).getByText("unknown")).toHaveAttribute("title", "HTTP 500 on the usage route");
    expect(other).toHaveClass("unwatched");
    expect(other.querySelector("[data-tick]")).toBeNull();
    expect(within(other).getByText("64 %")).toBeInTheDocument();
    expect(within(other).getByText("not watched")).toBeInTheDocument();
    expect(within(other).queryByText(/^(low|ok)$/)).toBeNull();
    expect(within(other).getByRole("meter")).toHaveAccessibleName("f***@example.invalid: 64 % remaining");
  });

  it("shows every account's figure and reset with no tick and no low or ok chip in Relay and Direct", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(1_800_000_000 * 1000));
    app.accounts = accountsFixture({ accounts: [
      accountFixture({ auth_index: "a1", email_masked: "o***@example.invalid", remaining_percent: 20, state: "low", reset_at: 1_800_000_000 + 72 * 60 }),
      accountFixture({ auth_index: "b2", email_masked: "t***@example.invalid", remaining_percent: 90, state: "ok", in_scope: false, reset_at: null }),
      accountFixture({ auth_index: "c3", email_masked: "s***@example.invalid", remaining_percent: null, state: "unknown", error: "HTTP 500 on the usage route" }),
    ] });
    for (const routing of ["relay", "direct"] as const) {
      app.status = statusFixture({ routing, decision: { relay_on: routing === "relay", reason: `routing: ${routing}` } });
      const view = render(Home, { ontab: noTab });
      const cards = [...document.querySelectorAll<HTMLElement>(".gmini")];
      expect(cards.map((c) => c.querySelector(".em")?.textContent), routing).toEqual(["o***@example.invalid", "t***@example.invalid", "s***@example.invalid"]);
      expect(document.querySelectorAll("[data-tick]"), routing).toHaveLength(0);
      expect(screen.queryByText(/^(low|ok|not watched)$/), routing).toBeNull();
      for (const c of cards) expect(c, routing).not.toHaveClass("unwatched");
      expect(within(cards[0]).getByText("20 %")).toBeInTheDocument();
      expect(within(cards[0]).getByText("resets in 1 h 12 min")).toBeInTheDocument();
      expect(within(cards[1]).getByText("90 %")).toBeInTheDocument();
      expect(within(cards[1]).getByText("reset time not read yet")).toBeInTheDocument();
      expect(within(cards[2]).getByText("unknown")).toHaveAttribute("title", "HTTP 500 on the usage route");
      expect(within(cards[0]).getByRole("meter")).toHaveAccessibleName("o***@example.invalid: 20 % remaining");
      view.unmount();
    }
  });

  it("shows the accounts as gauges in every mode", async () => {
    const ontab = vi.fn();
    const rows = [
      accountFixture({ auth_index: "a1", email_masked: "o***@example.invalid", remaining_percent: 20, state: "low" }),
      accountFixture({ auth_index: "b2", email_masked: "t***@example.invalid", remaining_percent: 90, state: "ok" }),
      accountFixture({ auth_index: "c3", email_masked: "s***@example.invalid", remaining_percent: null, state: "unknown" }),
      accountFixture({ auth_index: "d4", email_masked: "f***@example.invalid", remaining_percent: 64, state: "unknown", in_scope: false }),
      accountFixture({ auth_index: "e5", email_masked: "r***@example.invalid", remaining_percent: 20, state: "ok", reset_at: 1 }),
    ];
    app.accounts = accountsFixture({ accounts: rows });
    const on = render(Home, { ontab });
    const meters = screen.getAllByRole("meter");
    expect(meters).toHaveLength(5);
    expect(meters[0]).toHaveAttribute("aria-valuenow", "20");
    expect(screen.getByRole("meter", { name: /^o\*\*\*@example\.invalid: 20 % remaining, low below 30 %$/ })).toBe(meters[0]);   // named by its account
    expect(meters[2]).toHaveAccessibleName("s***@example.invalid: no reading");
    expect(meters.map((m) => m.querySelector(".fill")?.className.replace("fill ", ""))).toEqual(["tone-low", "tone-ok", undefined, "tone-ok", "tone-off"]);
    expect(meters[0].querySelector("[data-tick]")).toHaveStyle({ left: "30%" });          // the status fixture's threshold
    expect(meters[1]).toHaveAttribute("aria-valuenow", "90");
    expect(meters[2]).not.toHaveAttribute("aria-valuenow");
    expect(screen.getByText("no reading")).toBeInTheDocument();
    expect(screen.getByText("not watched")).toBeInTheDocument();                        // in_scope false, not the `unknown` the API gives it
    expect(meters[3]).toHaveAttribute("aria-valuenow", "100");           // the watched ones first
    expect(screen.getByText("window reset")).toBeInTheDocument();
    expect(screen.getByText("o***@example.invalid")).toBeInTheDocument();
    on.unmount();

    for (const routing of ["relay", "direct"] as const) {
      app.status = statusFixture({ routing, decision: { relay_on: routing === "relay", reason: `routing: ${routing}` } });
      const view = render(Home, { ontab });
      expect(screen.getAllByRole("meter")).toHaveLength(5);
      view.unmount();
    }

    app.status = statusFixture();
    app.accounts = accountsFixture({ read_at: null, age_s: null, accounts: rows });
    const unread = render(Home, { ontab });
    expect(screen.getByText("Reading the quota…")).toBeInTheDocument();
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
    unread.unmount();

    app.accounts = accountsFixture({ accounts: [] });
    render(Home, { ontab });
    expect(screen.getByText("No account yet")).toBeInTheDocument();
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
    await fireEvent.click(screen.getByRole("button", { name: "Add an account" }));
    expect(ontab).toHaveBeenLastCalledWith("accounts");
  });

  it("opens Activity from the 24 h bars", async () => {
    const ontab = vi.fn();
    render(Home, { ontab });
    await fireEvent.click(within(screen.getByRole("region", { name: "Last 24 h" })).getByRole("button", { name: "Open Activity" }));
    expect(ontab).toHaveBeenCalledWith("activity");
  });

  it("draws the last 24 h from a 24 h answer only, and says No requests yet when it is empty", () => {
    const busy = activityFixture({}, { 3: { relay: 5, passthrough: 2, errors: 1 }, 20: { relay: 7, images: 1 } });
    app.activity = busy;
    const first = render(Home, { ontab: noTab });
    const chart = screen.getByRole("region", { name: "Last 24 h" });
    expect(chart.querySelectorAll("[data-bucket]")).toHaveLength(24);
    for (const [name, n] of [["Relay", busy.totals.relay], ["Pass-through", busy.totals.passthrough], ["Images", busy.totals.images], ["Errors, inside the bars", busy.totals.errors]]) {
      expect(within(chart).getByText(name).parentElement).toHaveTextContent(String(n));
    }
    expect(chart.querySelectorAll("[data-errors]")).toHaveLength(1);                      // the one bucket with an error, marked above its bar
    expect(chart.querySelector("[data-bucket='3']")).toHaveAttribute("data-errors", "1");
    expect(chart.querySelectorAll("rect.s-err")).toHaveLength(1);                         // the mark itself, on the bucket with the error
    expect(chart.querySelector("[data-bucket='3'] rect.s-err")).not.toBeNull();
    expect(within(chart).getAllByText(/^\d\d:\d\d$/)).toHaveLength(4);                      // every sixth bucket
    expect(within(chart).queryByText("No requests yet")).toBeNull();
    first.unmount();

    app.activity = activityFixture({}, {}, "7d");
    const seven = render(Home, { ontab: noTab });
    expect(screen.queryByRole("img", { name: /Requests/ })).toBeNull();
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(0);
    seven.unmount();

    app.activity = activityFixture();
    render(Home, { ontab: noTab });
    const empty = screen.getByRole("region", { name: "Last 24 h" });
    expect(within(empty).getByText("No requests yet")).toBeInTheDocument();
    expect(empty.querySelector("[data-axes]")).not.toBeNull();
    expect(empty.querySelectorAll("[data-bucket] rect")).toHaveLength(0);
  });
});
