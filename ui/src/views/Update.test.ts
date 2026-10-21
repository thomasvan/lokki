import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { tick } from "svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Update from "./Update.svelte";
import { app } from "../lib/state.svelte";
import { updateLine, updateWords } from "../lib/update";

type S = NonNullable<typeof app.update>;
const status = (over: Partial<S>): S => ({
  state: "available", current_version: "0.1.0", version: "0.2.0",
  notes_url: "https://github.com/thomasvan/lokki/releases/tag/v0.2.0", can_install: true,
  reason: null, error: null, ...over,
});

describe("the update banner", () => {
  beforeEach(() => { app.update = null; delete window.lokki; });
  afterEach(() => { cleanup(); delete window.lokki; });

  it("renders per state", () => {
    // (label, status, text shown, text absent)
    const rows: Array<[string, Partial<S> | null, string[], string[]]> = [
      ["a plain browser", null, [], ["Check now", "available"]],
      ["idle", { state: "idle", version: null, notes_url: null }, ["lokki 0.1.0", "Check now"], ["available"]],
      ["up to date", { state: "up_to_date", version: null, notes_url: null }, ["lokki 0.1.0 is up to date", "Check now"], ["Install"]],
      ["available and installable", {}, ["lokki 0.2.0 is available", "Release notes", "Install and restart", "Check now"], ["Download"]],
      ["available, download only", { can_install: false, reason: "this lokki is not an AppImage" }, ["lokki 0.2.0 is available", "Download", "this lokki is not an AppImage"], ["Install and restart"]],
      ["checking", { state: "checking", version: null, notes_url: null }, ["Checking"], ["Check now"]],
      ["downloading", { state: "downloading" }, ["Downloading"], ["Install and restart"]],
      ["installing", { state: "installing" }, ["Installing"], ["Install and restart"]],
      ["error", { state: "error", version: null, notes_url: null, error: "the update check failed: 404" }, ["the update check failed: 404", "Check now"], ["Install"]],
      ["no build for this platform", { state: "up_to_date", version: null, notes_url: null, reason: "no build for this platform in the latest release" }, ["no build for this platform"], ["Install"]],
      ["an install that failed keeps the update", { state: "error", error: "the core (pid 4242) is still alive" }, ["4242", "Install and restart"], []],
    ];
    for (const [label, over, shown, absent] of rows) {
      app.update = over === null ? null : status(over);
      const { unmount } = render(Update);
      for (const t of shown) expect(screen.queryByText(t, { exact: false }), label).not.toBeNull();
      for (const t of absent) expect(screen.queryByText(t, { exact: false }), label).toBeNull();
      unmount();
    }
  });

  it("says its state through updateLine, word for word", () => {
    const rows: Array<[Partial<S>, string]> = [
      [{}, "lokki 0.2.0 is available."],
      [{ state: "error", error: "x" }, "lokki 0.2.0 is available."],         // a failed install keeps the update on offer
      [{ state: "error", version: null, notes_url: null, error: "x" }, "lokki 0.1.0"],
      [{ state: "idle", version: null, notes_url: null }, "lokki 0.1.0"],
      [{ state: "up_to_date", version: null, notes_url: null }, "lokki 0.1.0 is up to date."],
      [{ state: "checking" }, "Checking for an update…"],
      [{ state: "downloading" }, "Downloading the update…"],
      [{ state: "installing" }, "Installing the update; lokki restarts when it is done."],
    ];
    for (const [over, line] of rows) {
      expect(updateLine(status(over))).toBe(line);
      app.update = status(over);
      const { unmount } = render(Update);
      expect(screen.getByRole("status").textContent).toContain(line);
      unmount();
    }
  });

  it("hands the banner its line, reason and error through updateWords", () => {
    const rows: Array<[Partial<S>, { line: string; reason: string | null; error: string | null }]> = [
      [{}, { line: "lokki 0.2.0 is available.", reason: null, error: null }],
      [{ can_install: false, reason: "this lokki is not an AppImage" }, { line: "lokki 0.2.0 is available.", reason: "this lokki is not an AppImage", error: null }],
      [{ state: "up_to_date", version: null, reason: "no build for this platform in the latest release" }, { line: "lokki 0.1.0 is up to date.", reason: "no build for this platform in the latest release", error: null }],
      [{ state: "idle", reason: "kept by the shell" }, { line: "lokki 0.1.0", reason: null, error: null }],
      [{ state: "error", version: null, error: "the update check failed: 404" }, { line: "lokki 0.1.0", reason: null, error: "the update check failed: 404" }],
    ];
    for (const [over, words] of rows) {
      expect(updateWords(status(over))).toEqual(words);
      app.update = status(over);
      const { unmount } = render(Update);
      const bar = screen.getByRole("status");
      for (const t of [words.line, words.reason, words.error]) if (t) expect(bar.textContent).toContain(t);
      unmount();
    }
  });

  it("the buttons call the hooks and the state follows", async () => {
    let answer: (s: S) => void = () => {};
    const lokki = {
      installUpdate: vi.fn(async () => {}),
      checkForUpdate: vi.fn(() => new Promise<S>((resolve) => { answer = resolve; })),
      openExternal: vi.fn(async () => {}),
    };
    window.lokki = lokki;
    app.update = status({});
    render(Update);
    await fireEvent.click(screen.getByText("Release notes"));
    expect(lokki.openExternal).toHaveBeenCalledWith("https://github.com/thomasvan/lokki/releases/tag/v0.2.0");
    await fireEvent.click(screen.getByText("Install and restart"));
    expect(lokki.installUpdate).toHaveBeenCalledTimes(1);
    expect(app.update?.state).toBe("downloading");
    app.update = status({});
    await tick();
    await fireEvent.click(screen.getByText("Check now"));
    expect(lokki.checkForUpdate).toHaveBeenCalledTimes(1);
    expect(app.update?.state).toBe("checking");          // until the shell answers
    answer(status({ state: "up_to_date", version: null, notes_url: null }));
    await tick();
    // vi.waitFor gives up after 1 s by default: a hang catcher on a shared host, as in test-setup.ts
    await vi.waitFor(() => expect(app.update?.state).toBe("up_to_date"), { timeout: 10_000 });
  });

  it.each(["Release notes", "Download"])("shows a rejected openExternal from %s in an alert", async (button) => {
    window.lokki = { openExternal: vi.fn(async () => { throw new Error("no browser to open"); }) };
    app.update = status(button === "Download" ? { can_install: false, reason: "this lokki is not an AppImage" } : {});
    render(Update);
    await fireEvent.click(screen.getByText(button));
    expect((await screen.findByRole("alert")).textContent).toContain("no browser to open");
  });

  it("an install that rejects shows the sentence and keeps the button", async () => {
    window.lokki = { installUpdate: vi.fn(async () => { throw new Error("the core (pid 4242) is still alive"); }) };
    app.update = status({});
    render(Update);
    await fireEvent.click(screen.getByText("Install and restart"));
    expect(await screen.findByText("the core (pid 4242) is still alive", { exact: false })).toBeTruthy();
    expect(screen.queryByText("Install and restart")).not.toBeNull();
  });
});
