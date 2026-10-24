import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { accountsFixture, activityFixture, callsTo, hangs, schemaRows, settingsFixture, statusFixture } from "./test-helpers";
import { token } from "./lib/api";
import { UNWRITABLE, headline } from "./lib/mode";
import { app } from "./lib/state.svelte";
import App from "./App.svelte";

const ENDPOINT = { urls: ["http://127.0.0.1:8381/v1"], key_masked: "…abcd" };
const BODIES: Record<string, unknown> = {
  "/api/status": statusFixture(),
  "/api/endpoint": ENDPOINT,
  "/api/accounts": accountsFixture(),
  "/api/activity?window=24h": activityFixture(),
  "/api/schema": schemaRows,
  "/api/settings": settingsFixture(),
};
const DECIDED = headline(false);                      // the top bar's headline for the fixture's decision
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// A core that answers every route, and answers 401 to a call without `gate.need` in X-Relay-Token.
const gate: { need?: string } = {};
function core(need?: string, over: Record<string, unknown> = {}) {
  gate.need = need;
  const bodies = { ...BODIES, ...over };
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    const sent = (init?.headers as Record<string, string> | undefined)?.["X-Relay-Token"];
    if (gate.need && sent !== gate.need) return Promise.resolve(json(401, { error: { message: "missing or invalid operator token" } }));
    return Promise.resolve(path in bodies ? json(200, bodies[path]) : json(404, { error: { message: "not found" } }));
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("App", () => {
  beforeEach(() => {
    Object.assign(app, { status: null, endpoint: null, accounts: null, schema: null, settings: null, error: null, busy: false });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); token.set(null); });

  it("has the mark, the wordmark and the destinations", async () => {
    core();
    render(App);
    const heading = screen.getByRole("heading", { level: 1, name: "Lokki" });
    expect(heading.querySelector("svg[data-mark]")).toHaveAttribute("aria-hidden", "true");
    const nav = screen.getByRole("navigation", { name: "Destinations" });
    const names = ["Home", "Accounts", "Activity", "Settings"];
    expect(within(nav).getAllByRole("button").map((b) => b.textContent?.trim())).toEqual(names);
    const current = () => within(nav).getAllByRole("button").filter((b) => b.getAttribute("aria-current") === "page").map((b) => b.textContent?.trim());
    expect(current()).toEqual(["Home"]);
    await fireEvent.click(within(nav).getByRole("button", { name: "Accounts" }));
    expect(current()).toEqual(["Accounts"]);
    expect(await screen.findByText(DECIDED)).toBeInTheDocument();
  });

  // The run in the browser guards the whole chain; this one fails if the prompt, the stored token or the retry go missing.
  it("shows the token prompt on a 401 and retries with the token it was given", async () => {
    const fetch = core("op-1");
    render(App);
    const input = await screen.findByLabelText("Operator token");
    expect(input).toHaveAttribute("type", "password");
    expect(screen.queryByText(DECIDED)).toBeNull();
    await fireEvent.input(input, { target: { value: "op-1" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(await screen.findByText(DECIDED)).toBeInTheDocument();
    expect(sessionStorage.getItem("lokki.token")).toBe("op-1");
    expect(fetch.mock.calls.at(-1)?.[1]).toMatchObject({ headers: { "X-Relay-Token": "op-1" } });
    expect(screen.queryByLabelText("Operator token")).toBeNull();
    expect(document.body.innerHTML).not.toContain("op-1");
  });

  it("stores the token only once the core has accepted it", async () => {
    const fetch = core("op-1");
    render(App);
    const input = await screen.findByLabelText("Operator token");
    await fireEvent.input(input, { target: { value: "wrong" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    await screen.findByText("missing or invalid operator token");
    expect(sessionStorage.getItem("lokki.token")).toBeNull();
    const sent = fetch.mock.calls.map((c) => ((c[1] as RequestInit).headers as Record<string, string>)["X-Relay-Token"]);
    expect(sent).toContain("wrong");                              // the check carried the typed token
    expect(screen.getByLabelText("Operator token")).toHaveValue("wrong");

    await fireEvent.input(screen.getByLabelText("Operator token"), { target: { value: "op-1" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    await screen.findByText(DECIDED);
    expect(sessionStorage.getItem("lokki.token")).toBe("op-1");
  });

  it("keeps the views and their unsaved edits behind the prompt when a 401 comes mid-session", async () => {
    core();
    render(App);
    await screen.findByText("http://127.0.0.1:8381/v1");
    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const field = await screen.findByLabelText("Read every");
    await fireEvent.input(field, { target: { value: "90" } });

    gate.need = "op-2";                                            // the token expires
    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toContainElement(screen.getByLabelText("Operator token"));
    expect(screen.getByLabelText("Read every")).toHaveValue(90);

    await fireEvent.input(screen.getByLabelText("Operator token"), { target: { value: "op-2" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(screen.getByLabelText("Read every")).toHaveValue(90);
  });

  it("keeps the prompt and says why when the token is refused, and holds Use while it asks", async () => {
    core("op-1");
    render(App);
    await fireEvent.input(await screen.findByLabelText("Operator token"), { target: { value: "wrong" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(await screen.findByText("missing or invalid operator token")).toBeInTheDocument();
    expect(screen.getByLabelText("Operator token")).toBeInTheDocument();

    hangs();
    expect(screen.getByLabelText("Operator token")).toHaveValue("wrong");
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    expect(await screen.findByRole("button", { name: "Checking…" })).toBeDisabled();
  });

  it("disables Use until a token is typed, and says why", async () => {
    core("op-1");
    render(App);
    const use = await screen.findByRole("button", { name: "Use" });
    expect(use).toBeDisabled();
    expect(use).toHaveAccessibleDescription("paste the operator token the core was started with");
  });

  it("shows the API's message and the prompt on a 403", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(403, { error: { message: "the Origin is not this relay's own" } }))));
    render(App);
    expect(await screen.findByText("the Origin is not this relay's own")).toBeInTheDocument();
    expect(screen.getByLabelText("Operator token")).toBeInTheDocument();
  });

  it("says the core is not answering and offers Retry", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("failed"))));
    render(App);
    expect(await screen.findByText("the core is not answering")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    core();
    await fireEvent.click(retry);
    expect(await screen.findByText(DECIDED)).toBeInTheDocument();
    expect(screen.queryByText("the core is not answering")).toBeNull();
  });

  it("shows any other failure as the API words it", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(json(500, { error: { message: "internal error" } }))));
    render(App);
    expect(await screen.findByRole("alert")).toHaveTextContent("internal error");
  });

  it("offers Retry on Settings when the settings read fails, and the view follows", async () => {
    const fetch = core();
    let failing = true;
    const answer = fetch.getMockImplementation()!;
    fetch.mockImplementation((path: string, init?: RequestInit) =>
      failing && path === "/api/settings" ? Promise.resolve(json(500, { error: { message: "settings unreadable" } })) : answer(path, init));
    render(App);
    await screen.findByText("http://127.0.0.1:8381/v1");
    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    const failed = await screen.findByText(/^The settings could not be read: settings unreadable/);
    failing = false;
    await fireEvent.click(within(failed).getByRole("button", { name: "Retry" }));
    expect(await screen.findByLabelText("Cut when")).toBeInTheDocument();
    expect(screen.queryByText(/settings unreadable/)).toBeNull();
  });

  // The browser run re-reads Settings by leaving and re-entering it. The first entry reads the
  // settings on any screen, since the mode control in the top bar follows `writable`.
  it("reads each view's data when its tab is entered, again on every entry", async () => {
    const fetch = core();
    render(App);
    await screen.findByText("http://127.0.0.1:8381/v1");
    expect(callsTo(fetch, "/api/endpoint")).toBe(1);
    expect(callsTo(fetch, "/api/accounts")).toBeGreaterThan(0);
    await waitFor(() => expect(callsTo(fetch, "/api/activity?window=24h")).toBe(1));
    expect(callsTo(fetch, "/api/schema")).toBe(0);
    await waitFor(() => expect(callsTo(fetch, "/api/settings")).toBe(1));

    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(callsTo(fetch, "/api/activity?window=24h")).toBe(1);        // Home's read, not Settings'
    await waitFor(() => expect(callsTo(fetch, "/api/schema")).toBe(1));
    await waitFor(() => expect(callsTo(fetch, "/api/settings")).toBe(2));
    expect(await screen.findByLabelText("Cut when")).toBeInTheDocument();

    const accountsBefore = callsTo(fetch, "/api/accounts");
    await fireEvent.click(screen.getByRole("button", { name: "Accounts" }));
    await waitFor(() => expect(callsTo(fetch, "/api/accounts")).toBeGreaterThan(accountsBefore));
    expect(callsTo(fetch, "/api/schema")).toBe(1);
    expect(callsTo(fetch, "/api/settings")).toBe(2);         // read once and held: no read on a screen that only shows the control

    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await waitFor(() => expect(callsTo(fetch, "/api/schema")).toBe(2));
    await waitFor(() => expect(callsTo(fetch, "/api/settings")).toBe(3));
  });

  it("reads the settings after the token is accepted, and the mode control follows writable", async () => {
    const fetch = core("op-1", { "/api/settings": settingsFixture({ writable: false }) });
    render(App);
    await fireEvent.input(await screen.findByLabelText("Operator token"), { target: { value: "op-1" } });
    await fireEvent.click(screen.getByRole("button", { name: "Use" }));
    const relay = await screen.findByRole("radio", { name: "Relay" });
    await waitFor(() => expect(relay).toBeDisabled());
    expect(relay).toHaveAccessibleDescription(new RegExp(UNWRITABLE));
    const accepted = fetch.mock.calls
      .filter((c) => ((c[1] as RequestInit).headers as Record<string, string>)["X-Relay-Token"] === "op-1")
      .map((c) => c[0]);
    expect(accepted[0]).toBe("/api/status");
    expect(accepted).toContain("/api/settings");
  });

  it("polls the accounts every 30 s on their tab and stops on leaving it", async () => {
    vi.useFakeTimers();
    const fetch = core();
    render(App);
    await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole("button", { name: "Accounts" }));
    await vi.advanceTimersByTimeAsync(0);
    const first = callsTo(fetch, "/api/accounts");
    expect(first).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/accounts")).toBe(first + 1);

    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await vi.advanceTimersByTimeAsync(0);
    const left = callsTo(fetch, "/api/accounts");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(callsTo(fetch, "/api/accounts")).toBe(left);
  });

  it("reads the activity and the status on entering Activity, and polls the chosen window", async () => {
    vi.useFakeTimers();
    const fetch = core("", { "/api/activity?window=7d": activityFixture({}, {}, "7d") });
    render(App);
    await vi.advanceTimersByTimeAsync(0);
    await fireEvent.click(screen.getByRole("button", { name: "Activity" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByRole("heading", { name: "Activity" })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(24);
    const statusBefore = callsTo(fetch, "/api/status");
    expect(callsTo(fetch, "/api/activity?window=24h")).toBeGreaterThan(0);
    await fireEvent.click(screen.getByRole("button", { name: "7 d" }));
    await vi.advanceTimersByTimeAsync(0);
    expect(document.querySelectorAll("[data-bucket]")).toHaveLength(28);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(callsTo(fetch, "/api/activity?window=7d")).toBe(2);
    expect(callsTo(fetch, "/api/status")).toBeGreaterThan(statusBefore);
    app.activityWindow = "24h";
  });

  it("stops polling when the page goes away", async () => {
    vi.useFakeTimers();
    const fetch = core();
    const view = render(App);
    await vi.advanceTimersByTimeAsync(0);
    view.unmount();
    const calls = fetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch.mock.calls.length).toBe(calls);
  });
});
