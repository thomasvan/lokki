import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { modeCore, settingsFixture, statusFixture } from "../test-helpers";
import { ApiError } from "../lib/api";
import { app } from "../lib/state.svelte";
import TopBar from "./TopBar.svelte";

describe("TopBar", () => {
  beforeEach(() => {
    Object.assign(app, { status: statusFixture(), endpoint: null, accounts: null, schema: null, settings: settingsFixture(), error: null, busy: false,
      modeWrite: { flight: null, waiting: null, held: null, failure: null } });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  // The headlines are copied on purpose: the copy is the pin of spec 2's two sentences.
  it.each([
    [true, "every watched account is low", "Cutting every request"],
    [false, "routing: direct", "Passing requests through"],
  ])("states the decision with its reason verbatim (relay_on %s)", (relay_on, reason, headline) => {
    app.status = statusFixture({ decision: { relay_on, reason } });
    render(TopBar, { size: "compact" });
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(headline);
    expect(status).toContainElement(screen.getByText(reason));        // one element, the whole reason as given
  });

  // Before the first status read and when the core never answers there is no mode to control: the bar says which
  // of the two it is, in the UI's own sentence for an unreachable core, and offers no radio to write with.
  it.each([
    ["before the first read", null, "Reading the core's status"],
    ["when the core does not answer", new ApiError(0, "the core is not answering", "network"), "The core is not answering"],
    ["when the core takes too long", new ApiError(0, "the core did not answer in time", "network"), "The core is not answering"],
    ["when the core refuses the token", new ApiError(401, "missing or invalid operator token", "auth"), "Waiting for the operator token"],
    ["when the core answers with a failure", new ApiError(500, "internal error", "other"), "The core did not give its status"],
  ])("says what it waits for and offers no mode control %s", (_when, error, sentence) => {
    Object.assign(app, { status: null, settings: null, error });
    for (const size of ["full", "compact"] as const) {
      const view = render(TopBar, { size });
      expect(screen.getByRole("status")).toHaveTextContent(sentence);
      expect(screen.queryByRole("radiogroup")).toBeNull();
      expect(screen.queryByRole("radio")).toBeNull();
      expect(screen.queryByRole("slider")).toBeNull();
      view.unmount();
    }
  });

  const settle = async () => { for (let i = 0; i < 10; i++) await vi.advanceTimersByTimeAsync(0); };
  const puts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter((c) => c[1]?.method === "PUT").map((c) => c[1].body as string);
  const slider = () => screen.getByRole("slider", { name: "Mode and threshold" });
  const modes = () => screen.getByRole("radiogroup", { name: "Mode" });

  it("puts the mode slider under the choices on Home only", () => {
    const home = render(TopBar, { size: "full" });
    expect(modes().compareDocumentPosition(slider()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(screen.getByRole("banner")).getAllByRole("radio").map((r) => r.textContent?.trim())).toEqual(["Direct", "Auto 30 %", "Relay"]);
    home.unmount();
    render(TopBar, { size: "compact" });
    expect(modes()).toBeInTheDocument();
    expect(screen.queryByRole("slider")).toBeNull();
  });

  // WebKit does not focus a button on click, so nothing here blurs the slider or presses a pointer on it: the order
  // comes from the shared write (decision 21).
  it("a top-bar choice with a held slider position sends the held position first, then the choice, with no blur", async () => {
    vi.useFakeTimers();
    const core = modeCore("auto", 30);
    render(TopBar, { size: "full" });
    const blurred = vi.fn();
    slider().addEventListener("blur", blurred);
    slider().focus();
    for (let i = 0; i < 3; i++) await fireEvent.keyDown(slider(), { key: "ArrowLeft" });
    expect(slider()).toHaveValue("27");
    await fireEvent.click(within(modes()).getByRole("radio", { name: "Direct" }));
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":27}}', '{"routing":"direct"}']);
    await vi.advanceTimersByTimeAsync(1_000);
    await settle();
    expect(puts(core.fetch)).toHaveLength(2);             // the held position's timer was cancelled
    expect(app.status?.routing).toBe("direct");
    expect(core.state).toEqual({ routing: "direct", threshold: 27 });
    expect(within(modes()).getByRole("radio", { name: "Direct" })).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(slider());
    expect(blurred).not.toHaveBeenCalled();
  });

  it("says busy on every mode control while any of them writes, and keeps each enabled", async () => {
    vi.useFakeTimers();
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    render(TopBar, { size: "full" });
    const radios = () => within(modes()).getAllByRole("radio");
    for (const start of ["radio", "slider"] as const) {
      if (start === "radio") await fireEvent.click(within(modes()).getByRole("radio", { name: "Relay" }));
      else {
        slider().focus();
        await fireEvent.keyDown(slider(), { key: "PageDown" });
        await fireEvent.blur(slider());
      }
      await settle();
      expect(modes(), start).toHaveAttribute("aria-busy", "true");
      expect(slider(), start).toHaveAttribute("aria-busy", "true");
      if (start === "slider") {
        // A top-bar choice made while the slider's write is in flight waits; the slider keeps showing its own write.
        await fireEvent.click(within(modes()).getByRole("radio", { name: "Direct" }));
        expect(within(modes()).getByRole("radio", { name: "Direct" }).querySelector('[data-icon="spinner"]')).not.toBeNull();
        expect(slider()).toHaveValue("90");
        expect(document.querySelector("[data-readout] [data-icon='spinner']")).not.toBeNull();
      }
      for (const r of radios()) expect(r, start).toBeEnabled();
      expect(slider(), start).toBeEnabled();
      core.release();
      await settle();
      if (start === "slider") {
        expect(slider()).toHaveAttribute("aria-busy", "true");          // the waiting Direct, sent after it
        core.release();
        await settle();
      }
      expect(modes(), start).toHaveAttribute("aria-busy", "false");
      expect(slider(), start).toHaveAttribute("aria-busy", "false");
    }
    expect(puts(core.fetch)).toEqual(['{"routing":"relay"}', '{"routing":"auto","low_quota":{"threshold_percent":90}}', '{"routing":"direct"}']);
  });
});
