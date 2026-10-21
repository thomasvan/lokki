import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { modeCore, settingsFixture } from "../test-helpers";
import { MODES, UNWRITABLE } from "../lib/mode";
import { app, chooseMode, loadStatus } from "../lib/state.svelte";
import ModeSlider from "./ModeSlider.svelte";

// The fake timers drive the 500 ms after a run of key steps; each `settle` lets a write and its reads answer
// without moving the clock past that timer.
const settle = async () => { for (let i = 0; i < 10; i++) await vi.advanceTimersByTimeAsync(0); };
const slider = () => screen.getByRole("slider", { name: "Mode and threshold" });
const readout = () => document.querySelector<HTMLElement>("[data-readout]")!;
const words = () => document.querySelector<HTMLElement>(".slider-words")!;
const puts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter((c) => c[1]?.method === "PUT").map((c) => c[1].body as string);
const key = (k: string) => fireEvent.keyDown(slider(), { key: k });
const drag = async (...to: number[]) => {
  await fireEvent.pointerDown(slider(), { pointerId: 1 });
  for (const v of to) await fireEvent.input(slider(), { target: { value: String(v) } });
};
const AT_45 = '{"routing":"auto","low_quota":{"threshold_percent":45}}';
const REFUSED = "the settings file names no client token; the reload would refuse it, so nothing is written";

describe("ModeSlider", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.assign(app, {
      status: null, endpoint: null, accounts: null, schema: null, settings: settingsFixture(), error: null, busy: false,
      modeWrite: { flight: null, waiting: null, held: null, failure: null },
    });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

  // Review Focus 14
  it("writes once when a drag lets go, and nothing for the position the core reports", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    await drag(40, 30);
    await fireEvent.change(slider(), { target: { value: "30" } });
    await settle();
    expect(puts(core.fetch)).toEqual([]);                 // let go where the core already is
    await drag(40, 45);
    expect(slider()).toHaveValue("45");
    expect(slider()).toHaveAttribute("aria-valuetext", "Auto 45 %: Cut when every watched account has less than 45 % left · every model");
    expect(puts(core.fetch)).toEqual([]);                 // nothing while the pointer holds it
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([AT_45]);
    expect(app.status?.low_quota.threshold_percent).toBe(45);
  });

  // Review Focus 14
  it("writes once for a run of key steps, 500 ms after the last or on blur", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    for (let i = 0; i < 5; i++) {
      await key("ArrowRight");
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(slider()).toHaveValue("35");
    expect(words()).toHaveTextContent("Cut when every watched account has less than 35 % left · every model");
    await vi.advanceTimersByTimeAsync(399);               // 499 ms after the fifth step
    expect(puts(core.fetch)).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":35}}']);

    for (let i = 0; i < 3; i++) await key("ArrowRight");
    await fireEvent.blur(slider());
    await settle();
    expect(puts(core.fetch)).toHaveLength(2);             // at once, on the blur
    expect(puts(core.fetch)[1]).toBe('{"routing":"auto","low_quota":{"threshold_percent":38}}');
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toHaveLength(2);             // and the timer it replaced sends nothing
  });

  it("steps by 1, 10 with PageUp and PageDown, and to the ends with Home and End", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    for (const [k, value] of [
      ["ArrowRight", 31], ["ArrowUp", 32], ["ArrowLeft", 31], ["ArrowDown", 30], ["PageUp", 40], ["PageDown", 30],
    ] as const) {
      await key(k);
      expect(slider(), k).toHaveValue(String(value));
    }
    await vi.advanceTimersByTimeAsync(600);
    expect(puts(core.fetch)).toEqual([]);                 // back where the core is: nothing to write

    await key("End");
    expect(slider()).toHaveValue("100");
    expect(slider()).toHaveAttribute("aria-valuetext", `Relay: ${MODES[2].sentence}`);
    await key("PageUp");
    expect(slider()).toHaveValue("100");                  // no further than the end
    await vi.advanceTimersByTimeAsync(600);
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"relay"}']);
    await key("Home");
    expect(slider()).toHaveAttribute("aria-valuetext", `Direct: ${MODES[0].sentence}`);
    await key("PageDown");
    expect(slider()).toHaveValue("0");
    await vi.advanceTimersByTimeAsync(600);
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"relay"}', '{"routing":"direct"}']);
    expect(core.state.threshold).toBe(30);                // the ends leave the stored threshold
  });

  it("stays enabled, busy and focused while any mode write runs, and shows a waiting position as pending", async () => {
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    render(ModeSlider);
    slider().focus();
    void chooseMode("direct");                            // the top bar's write
    await settle();
    expect(slider()).toHaveAttribute("aria-busy", "true");
    expect(slider()).toBeEnabled();
    expect(document.activeElement).toBe(slider());
    expect(readout().querySelector('[data-icon="spinner"]')).toBeNull();     // not the slider's write

    await key("PageUp");
    await vi.advanceTimersByTimeAsync(600);               // its timer chooses 40, which waits
    expect(app.modeWrite.waiting).toEqual({ position: 40 });
    expect(slider()).toHaveValue("40");
    expect(readout().querySelector('[data-icon="spinner"]')).not.toBeNull();
    expect(puts(core.fetch)).toEqual(['{"routing":"direct"}']);
    core.release();
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"direct"}', '{"routing":"auto","low_quota":{"threshold_percent":40}}']);
    core.release();
    await settle();
    expect(slider()).toHaveAttribute("aria-busy", "false");
    expect(slider()).toHaveValue("40");
    expect(readout().querySelector('[data-icon="spinner"]')).toBeNull();
    expect(document.activeElement).toBe(slider());
  });

  it("moves with keys while busy, and an end reached then waits as a mode", async () => {
    const core = modeCore("auto", 30);
    core.ctl.hold = true;
    render(ModeSlider);
    slider().focus();
    await drag(45);
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    expect(slider()).toHaveAttribute("aria-busy", "true");
    await key("End");
    expect(slider()).toHaveValue("100");
    await vi.advanceTimersByTimeAsync(600);
    expect(app.modeWrite.waiting).toEqual({ position: 100 });
    core.release();
    await settle();
    core.release();
    await settle();
    expect(puts(core.fetch)).toEqual([AT_45, '{"routing":"relay"}']);
    expect(core.state).toEqual({ routing: "relay", threshold: 45 });
  });

  it("leaves a key with Alt, Ctrl or Meta to the browser", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    for (const mod of ["altKey", "ctrlKey", "metaKey"] as const) {
      const handled = !(await fireEvent.keyDown(slider(), { key: "ArrowLeft", [mod]: true }));
      expect(handled, mod).toBe(false);                    // not prevented
      expect(app.modeWrite.held, mod).toBeNull();
    }
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([]);
    expect(slider()).toHaveValue("30");
  });

  it("writes nothing while a drag is held, whatever the keys, the clock or the focus do, and once when it lets go", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    await drag(45);
    await vi.advanceTimersByTimeAsync(1_000);             // a drag held longer than 500 ms
    expect(puts(core.fetch)).toEqual([]);
    await key("ArrowRight");                               // a key step during the drag
    expect(slider()).toHaveValue("46");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([]);
    await fireEvent.blur(slider());                        // the focus leaves during the drag
    await settle();
    expect(puts(core.fetch)).toEqual([]);
    await fireEvent.change(slider(), { target: { value: "46" } });
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":46}}']);
  });

  it("writes a drag once when the capture is lost before the change, even when the write is refused", async () => {
    const core = modeCore("auto", 30);
    core.ctl.refuse = [[409, { error: { message: REFUSED } }]];
    render(ModeSlider);
    await drag(45);
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 1 }));
    await settle();
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([AT_45]);
    expect(screen.getByRole("alert")).toHaveTextContent(REFUSED);
  });

  it("writes a drag once when the change comes before the capture is lost, even when the write is refused", async () => {
    const core = modeCore("auto", 30);
    core.ctl.refuse = [[409, { error: { message: REFUSED } }]];
    render(ModeSlider);
    await drag(45);
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 1 }));
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([AT_45]);
    expect(screen.getByRole("alert")).toHaveTextContent(REFUSED);
  });

  it("ends a drag on pointerup when no change or capture loss follows, and writes its key step once", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    await fireEvent.pointerDown(slider(), { pointerId: 1 });
    await key("ArrowRight");
    expect(slider()).toHaveValue("31");
    await fireEvent.pointerUp(slider(), { pointerId: 1 });
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    await fireEvent.blur(slider());
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":31}}']);
    expect(app.modeWrite.held).toBeNull();
  });

  it("ends a drag only on its own pointer: a second pointer's release writes nothing", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    await drag(45);                                        // pointer 1
    await fireEvent.pointerUp(slider(), { pointerId: 2 });  // a second touch lets go
    await fireEvent.pointerCancel(slider(), { pointerId: 2 });
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 2 }));
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([]);
    expect(slider()).toHaveValue("45");
    await fireEvent.pointerUp(slider(), { pointerId: 1 });
    await settle();
    expect(puts(core.fetch)).toEqual([AT_45]);
  });

  it("ends a drag on its pointerup outside the slider, with no change and no capture loss", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    await drag(45);
    await fireEvent.pointerUp(window, { pointerId: 1 });
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([AT_45]);
  });

  it("writes nothing on a pointerup when no drag is down", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    await key("ArrowRight");                               // held, its 500 ms running
    await fireEvent.pointerUp(slider(), { pointerId: 1 });
    await fireEvent.pointerUp(window, { pointerId: 1 });
    await settle();
    expect(puts(core.fetch)).toEqual([]);
    expect(app.modeWrite.held).toBe(31);
    await vi.advanceTimersByTimeAsync(600);
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":31}}']);
  });

  it("drops a drag's held position when it goes away mid-drag", async () => {
    const core = modeCore("auto", 30);
    const view = render(ModeSlider);
    await drag(45);
    view.unmount();
    expect(app.modeWrite.held).toBeNull();
    await chooseMode("relay");                             // the next choice sends itself alone
    expect(puts(core.fetch)).toEqual(['{"routing":"relay"}']);
  });

  it("writes once when a drag is let go outside the window, and nothing when the browser cancels it", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    await drag(40, 45);
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 1 }));      // let go outside: no change event comes
    await settle();
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 1 }));      // a second loss writes nothing more
    await settle();
    expect(puts(core.fetch)).toEqual([AT_45]);

    await drag(60);
    expect(slider()).toHaveValue("60");
    await fireEvent(slider(), new PointerEvent("pointercancel", { pointerId: 1 }));
    await fireEvent(slider(), new PointerEvent("lostpointercapture", { pointerId: 1 }));
    await settle();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(puts(core.fetch)).toEqual([AT_45]);
    expect(slider()).toHaveValue("45");                    // back to what the core reports
  });

  it("rests on the last read position and says the read's error when the write succeeds but its read fails", async () => {
    const core = modeCore("auto", 30);
    core.ctl.statusFails = 1;
    render(ModeSlider);
    await drag(45);
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    expect(puts(core.fetch)).toEqual([AT_45]);
    expect(core.state.threshold).toBe(45);                // written
    expect(slider()).toHaveValue("30");                   // the last status read
    expect(screen.getByRole("alert")).toHaveTextContent("written, but the mode could not be read back: the status could not be built");
    await loadStatus();                                   // the next status read moves it
    expect(slider()).toHaveValue("45");
  });

  it("has no thumb and takes no input before a status is read", async () => {
    vi.stubGlobal("fetch", vi.fn());
    render(ModeSlider);
    expect(slider()).toBeDisabled();
    expect(slider()).toHaveAttribute("data-empty");
    expect(slider()).not.toHaveAttribute("aria-valuetext");
    expect(readout()).toHaveTextContent(/^$/);
    expect(words()).toHaveTextContent(/^$/);
    await key("End");
    await vi.advanceTimersByTimeAsync(1_000);
    expect(app.modeWrite.held).toBeNull();
    expect(vi.mocked(fetch)).not.toHaveBeenCalled();
  });

  it("shows the position held or stepped to until it writes, whatever a status read says", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    slider().focus();
    for (let i = 0; i < 3; i++) await key("ArrowRight");
    await loadStatus();                                   // the 10 s poll lands with 30
    expect(slider()).toHaveValue("33");
    expect(readout()).toHaveTextContent("Auto 33 %");
    await vi.advanceTimersByTimeAsync(600);
    await settle();
    expect(puts(core.fetch)).toEqual(['{"routing":"auto","low_quota":{"threshold_percent":33}}']);
    expect(slider()).toHaveValue("33");
  });

  it("goes back to what the core reports and says why when the write is refused or fails", async () => {
    const core = modeCore("auto", 30);
    render(ModeSlider);
    const alert = () => screen.queryByRole("alert");

    core.ctl.refuse = [[409, { error: { message: REFUSED } }]];
    await drag(45);
    await fireEvent.change(slider(), { target: { value: "45" } });
    await settle();
    expect(slider()).toHaveValue("30");
    expect(alert()).toHaveTextContent(REFUSED);
    expect(app.error).toBeNull();                          // a refusal is the slider's to say, not the shell's

    core.ctl.refuse = ["network"];
    await drag(50);
    await fireEvent.change(slider(), { target: { value: "50" } });
    await settle();
    expect(slider()).toHaveValue("30");
    expect(alert()).toHaveTextContent("the core is not answering");
    app.error = null;

    // A 409 with a choice waiting behind it: the line stays while the waiting write runs, then gives way to its result.
    for (const [then, line] of [[null, null], [[409, { error: { message: "settings are read-only here: /x/config.yaml is not writable" } }],
      "settings are read-only here: /x/config.yaml is not writable"]] as const) {
      core.state.routing = "auto";
      core.state.threshold = 30;
      await loadStatus();
      core.ctl.hold = true;
      core.ctl.refuse = then ? [[409, { error: { message: REFUSED } }], then] : [[409, { error: { message: REFUSED } }]];
      await drag(45);
      await fireEvent.change(slider(), { target: { value: "45" } });
      await settle();
      await drag(55);
      await fireEvent.change(slider(), { target: { value: "55" } });
      core.release();
      await settle();
      expect(alert(), String(line)).toHaveTextContent(REFUSED);     // the waiting write runs; the line stays
      expect(puts(core.fetch).at(-1)).toBe('{"routing":"auto","low_quota":{"threshold_percent":55}}');
      core.release();
      await settle();
      core.ctl.hold = false;
      if (line === null) {
        expect(alert()).toBeNull();
        expect(slider()).toHaveValue("55");
      } else {
        expect(alert()).toHaveTextContent(line);
        expect(slider()).toHaveValue("30");
      }
    }
    await vi.advanceTimersByTimeAsync(10_000);
    expect(alert()).toHaveTextContent("settings are read-only here");   // kept until the next write
  });

  it("is disabled with the lock line when the settings file is not writable", () => {
    modeCore("auto", 30);
    app.settings = settingsFixture({ writable: false });
    render(ModeSlider);
    expect(slider()).toBeDisabled();
    expect(slider()).toHaveAccessibleDescription(UNWRITABLE);
    expect(screen.getByText(UNWRITABLE)).toBeInTheDocument();
  });
});
