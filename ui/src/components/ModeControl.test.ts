import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { settingsFixture, statusFixture } from "../test-helpers";
import type { Status } from "../lib/api";
import { MODES, UNWRITABLE, modeLabel, type Routing } from "../lib/mode";
import { app, loadStatus } from "../lib/state.svelte";
import ModeControl from "./ModeControl.svelte";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// A core that applies each PUT's routing to what it reports. `hold` keeps the next PUT
// unanswered until `release` is called; `refuse` answers the next PUT with that error.
function core(start: Routing) {
  const state = { routing: start };
  const report = (): Status => statusFixture({ routing: state.routing });
  const ctl: { hold: boolean; release?: () => void; refuse?: [number, unknown]; reported?: Routing } = { hold: false };
  const fetch = vi.fn((path: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      if (ctl.refuse) return Promise.resolve(json(...ctl.refuse));
      const answer = () => {
        const body = JSON.parse(String(init.body)) as { routing?: Routing };
        if (body.routing) state.routing = body.routing;
        if (ctl.reported) state.routing = ctl.reported;
        return json(200, { applied: Object.keys(body), restart_required: [], comments_dropped: false });
      };
      if (!ctl.hold) return Promise.resolve(answer());
      return new Promise<Response>((done) => { ctl.release = () => done(answer()); });
    }
    if (path === "/api/settings") return Promise.resolve(json(200, settingsFixture()));
    return Promise.resolve(json(200, report()));
  });
  vi.stubGlobal("fetch", fetch);
  app.status = report();
  return { fetch, ctl };
}

const calls = (fetch: ReturnType<typeof vi.fn>) =>
  fetch.mock.calls.map((c) => [(c[1] as RequestInit | undefined)?.method ?? "GET", c[0], (c[1] as RequestInit | undefined)?.body]);
// Auto is named by its label, which carries the fixture status's threshold.
const AUTO = "Auto 30 %";
const radio = (name: string) => screen.getByRole("radio", { name: name === "Auto" ? AUTO : name });
const labelOf = (mode: Routing) => modeLabel(mode, 30);
const group = () => screen.getByRole("radiogroup", { name: "Mode" });

describe("ModeControl", () => {
  beforeEach(() => {
    Object.assign(app, {
      status: null, endpoint: null, accounts: null, schema: null, settings: settingsFixture(), error: null, busy: false,
      modeWrite: { flight: null, waiting: null, held: null, failure: null },
    });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("writes each mode's body in one PUT, and only routing", async () => {
    const { fetch } = core("auto");
    render(ModeControl);
    for (const [name, body] of [
      ["Direct", '{"routing":"direct"}'],
      ["Auto", '{"routing":"auto"}'],
      ["Relay", '{"routing":"relay"}'],
    ]) {
      fetch.mockClear();
      await fireEvent.click(radio(name));
      await waitFor(() => expect(radio(name)).toHaveAttribute("aria-checked", "true"));
      expect(calls(fetch)).toEqual([["PUT", "/api/settings", body], ["GET", "/api/settings", undefined], ["GET", "/api/status", undefined]]);
    }
    fetch.mockClear();
    await fireEvent.click(radio("Relay"));
    expect(fetch).not.toHaveBeenCalled();
  });

  it("lists Direct, Auto with the threshold the core reports, and Relay", async () => {
    const { fetch } = core("direct");
    app.status = statusFixture({ routing: "direct", low_quota: { ...statusFixture().low_quota, threshold_percent: 45 } });
    const first = render(ModeControl);
    expect(screen.getAllByRole("radio").map((r) => r.textContent?.trim())).toEqual(["Direct", "Auto 45 %", "Relay"]);
    await fireEvent.click(screen.getByRole("radio", { name: "Auto 45 %" }));
    await waitFor(() => expect(screen.getByRole("radio", { name: "Auto 30 %" })).toHaveAttribute("aria-checked", "true"));
    expect(calls(fetch)[0]).toEqual(["PUT", "/api/settings", '{"routing":"auto"}']);     // no threshold
    first.unmount();
    expect([modeLabel("direct", 30), modeLabel("auto", 30), modeLabel("relay", 30)]).toEqual(["Direct", "Auto 30 %", "Relay"]);
  });

  it("follows the read back, not the click", async () => {
    const { ctl } = core("auto");
    ctl.hold = true;
    ctl.reported = "auto";                       // the core keeps Auto whatever was asked
    render(ModeControl);
    await fireEvent.click(radio("Direct"));
    expect(radio("Direct")).toHaveAttribute("aria-checked", "false");
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    expect(group()).toHaveAttribute("aria-busy", "true");
    ctl.release!();
    await waitFor(() => expect(group()).toHaveAttribute("aria-busy", "false"));
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    expect(radio("Direct")).toHaveAttribute("aria-checked", "false");
  });

  it("keeps the choice busy through a status poll that lands during the write, and sends a second choice after it", async () => {
    const { fetch, ctl } = core("auto");
    ctl.hold = true;
    render(ModeControl);
    await fireEvent.click(radio("Direct"));
    await loadStatus();                          // the 10 s poll, answering the mode before the write
    expect(group()).toHaveAttribute("aria-busy", "true");
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    await fireEvent.click(radio("Relay"));       // a second choice while the first is in flight waits, marked pending
    expect(calls(fetch).filter((c) => c[0] === "PUT")).toEqual([["PUT", "/api/settings", '{"routing":"direct"}']]);
    expect(radio("Relay").querySelector('[data-icon="spinner"]')).not.toBeNull();
    expect(radio("Relay")).toHaveAttribute("aria-checked", "false");
    ctl.hold = false;
    ctl.release!();
    await waitFor(() => expect(radio("Relay")).toHaveAttribute("aria-checked", "true"));
    expect(group()).toHaveAttribute("aria-busy", "false");
    expect(calls(fetch).filter((c) => c[0] === "PUT").map((c) => c[2])).toEqual(['{"routing":"direct"}', '{"routing":"relay"}']);
  });

  // Every choice is a write, so the arrows only move the focus (not the ARIA radio pattern's
  // check-on-arrow); Space or Enter chooses, which a real browser turns into a click: the e2e
  // binds that half.
  it("moves the focus with the arrows, Home and End, writes nothing, and keeps one tab stop", async () => {
    const { fetch } = core("auto");
    render(ModeControl);
    expect(MODES.map((m) => radio(labelOf(m.mode)).tabIndex)).toEqual([-1, 0, -1]);
    radio("Auto").focus();
    for (const [key, focused] of [
      ["ArrowRight", "Relay"], ["ArrowRight", "Direct"], ["ArrowDown", "Auto"], ["ArrowLeft", "Direct"],
      ["ArrowUp", "Relay"], ["Home", "Direct"], ["End", "Relay"],
    ]) {
      await fireEvent.keyDown(document.activeElement!, { key });
      expect(document.activeElement, key).toBe(radio(focused));
    }
    expect(fetch).not.toHaveBeenCalled();
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    expect(MODES.map((m) => radio(labelOf(m.mode)).tabIndex)).toEqual([-1, 0, -1]);
  });

  // The key shows beside the message, after a space, unless the message names it already.
  it.each([
    ["Auto", "direct" as Routing, 400, "routing must be one of relay, auto, direct", "routing", null],
    ["Direct", "auto" as Routing, 409, "settings are read-only here: /x/config.yaml is not writable", "routing", "routing"],
  ])("shows a refused write beside the control and keeps the mode (%s from %s, %i)", async (name, start, status, message, key, shown) => {
    const { fetch, ctl } = core(start);
    ctl.refuse = [status, { error: { message }, key }];
    render(ModeControl);
    const before = labelOf(start);
    await fireEvent.click(radio(name));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(shown ? `${message} ${shown}` : message);
    expect(alert.querySelector("code")?.textContent ?? null).toBe(shown);
    expect(radio(before)).toHaveAttribute("aria-checked", "true");
    expect(radio(name)).toHaveAttribute("aria-checked", "false");
    expect(group()).toHaveAttribute("aria-busy", "false");
    expect(calls(fetch).map((c) => c[0])).toEqual(["PUT"]);
    expect(app.error).toBeNull();
  });

  it("is disabled where the settings file is not writable, and enabled while that is unknown", async () => {
    core("auto");
    app.settings = settingsFixture({ writable: false });
    const first = render(ModeControl);
    for (const { mode } of MODES) {
      expect(radio(labelOf(mode))).toBeDisabled();
      expect(radio(labelOf(mode))).toHaveAccessibleDescription(new RegExp(UNWRITABLE));
    }
    expect(screen.getByText(UNWRITABLE)).toBeInTheDocument();
    first.unmount();

    app.settings = null;
    render(ModeControl);
    for (const { mode } of MODES) expect(radio(labelOf(mode))).toBeEnabled();
    expect(screen.queryByText(UNWRITABLE)).toBeNull();
  });

  it("keeps the check on the mode the core reports, with a waiting choice's spinner beside its label", async () => {
    const { ctl } = core("auto");
    ctl.hold = true;
    render(ModeControl);
    await fireEvent.click(radio("Direct"));                  // in flight
    await fireEvent.click(radio("Auto"));                    // back to Auto, waiting behind it
    expect(radio("Auto")).toHaveAttribute("aria-checked", "true");
    expect(radio("Auto").querySelector('[data-icon="check"]')).not.toBeNull();
    expect(radio("Auto").querySelector('[data-icon="spinner"]')).not.toBeNull();
    expect(radio("Direct").querySelector('[data-icon="spinner"]')).not.toBeNull();
    expect(radio("Direct").querySelector('[data-icon="check"]')).toBeNull();
    ctl.hold = false;
    ctl.release!();
    await waitFor(() => expect(group()).toHaveAttribute("aria-busy", "false"));
  });

  it("marks the checked mode with a check icon, not colour alone", () => {
    core("relay");
    render(ModeControl);
    expect(radio("Relay").querySelector('[data-icon="check"]')).not.toBeNull();
    for (const name of ["Auto", "Direct"]) expect(radio(name).querySelector('[data-icon="check"]')).toBeNull();
  });

  it("says each mode's sentence word for word, and none says free or names a saving", () => {
    core("auto");
    // The copy is the pin: spec 3.1's sentences, so a change to them takes two deliberate edits.
    expect(MODES.map((m) => m.sentence)).toEqual([
      "Every request goes to CLIProxyAPI untouched, at full price.",
      "Cut when the watched accounts run low; otherwise pass through at full price.",
      "Every request is cut: Lokki ends the answer early so it costs less.",
    ]);
    render(ModeControl);
    for (const { mode, sentence } of MODES) expect(radio(labelOf(mode))).toHaveAccessibleDescription(sentence);
    for (const { label, sentence } of MODES) {
      expect(`${label} ${sentence}`).not.toMatch(/\bfree\b|\bsav(e|ed|es|ing)\b/i);
    }
  });
});
