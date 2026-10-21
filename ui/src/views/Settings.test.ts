import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { accountFixture, accountsFixture, answers, schemaRows, settingsFixture, statusFixture } from "../test-helpers";
import { ApiError, getModels } from "../lib/api";
import { MODES, NO_SECRET, NO_SECRET_AUTO, UNWRITABLE } from "../lib/mode";
import { PANEL_PATH } from "../lib/panel";
import { app, loadAccounts, loadSettings } from "../lib/state.svelte";
import { updateLine, updateWords } from "../lib/update";
import { VERSION } from "../lib/version";
import Settings from "./Settings.svelte";

// Rendered under Auto, Settings reads the model list as it opens. It is the one read that is not part of a test's
// own order of requests, so `getModels` is a mock the models tests drive, and the other tests see an empty list.
vi.mock("../lib/api", async (original) => ({ ...(await original<typeof import("../lib/api")>()), getModels: vi.fn() }));
const saved = (over: { applied?: string[]; restart_required?: string[] } = {}) =>
  [200, { applied: [], restart_required: [], comments_dropped: false, ...over }] as [number, unknown];
const rereads = (): Array<[number, unknown]> => [[200, settingsFixture()], [200, statusFixture()]];

const region = (name: string) => screen.getByRole("region", { name });
const typeInto = (label: string, value: string) => fireEvent.input(screen.getByLabelText(label), { target: { value } });
const saveButton = (name: string) => within(region(name)).getByRole("button", { name: /^(Save \d+ changes?|No changes|Saving…)$/ });
const save = (name: string) => fireEvent.click(saveButton(name));
const body = (fetch: ReturnType<typeof vi.fn>, call: number) => JSON.parse(fetch.mock.calls[call][1].body);
const calls = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.map((c) => [c[1]?.method ?? "GET", c[0]]);
const withCliproxy = (cliproxy: Record<string, unknown>) => settingsFixture({ cliproxy: { ...settingsFixture().cliproxy, ...cliproxy } });
const rendered = () => render(Settings, { ontab: () => {} });
const SECRET = "/data/secrets/cliproxy-mgmt.secret";          // the fixture has none; a count needs a core that reads
const slider = () => within(region("Mode")).getByRole("slider", { name: "Mode and threshold" });
// The slider's key steps write on its blur, at once, without the 500 ms timer.
async function slide(...keys: string[]) {
  slider().focus();
  for (const key of keys) await fireEvent.keyDown(slider(), { key });
  await fireEvent.blur(slider());
}

describe("Settings", () => {
  beforeEach(() => {
    vi.mocked(getModels).mockReset().mockResolvedValue({ models: [] });
    Object.assign(app, {
      status: statusFixture(), endpoint: null, accounts: null, schema: schemaRows, settings: settingsFixture(), error: null, busy: false,
      restart: { core: false, cliproxy: false }, failed: { schema: null, settings: null, accounts: null },
      modeWrite: { flight: null, waiting: null, held: null, failure: null },
    });
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); delete window.lokki; });

  it("labels each field from the schema and its choices from choice_labels", () => {
    app.settings = settingsFixture({ cli_proxy_api_key: "***", auth: { tokens: [{ name: "laptop", token: "***", operator: true }] } });
    rendered();
    const titles = ["Mode", "Connection", "Updates and About", "Advanced", "CLIProxyAPI", "Quota reading", "Cutting", "Tool calls", "Traffic", "Set in the file"];
    const places = titles.map((n) => region(n));
    places.slice(1).forEach((el, i) => {
      expect(places[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    });

    expect(screen.queryByLabelText("Cut below, % left")).toBeNull();          // the slider sets the threshold
    expect(slider()).toHaveValue(String(statusFixture().low_quota.threshold_percent));
    const rule = screen.getByLabelText("Cut when");
    expect(within(rule).getAllByRole("option").map((o) => o.textContent)).toEqual(["every watched account is low", "any watched account is low"]);
    expect(screen.getByLabelText("Address")).toHaveValue("127.0.0.1:8371");
    expect(within(region("Cutting")).getByLabelText("Answer budget")).toBeInTheDocument();
    expect(within(region("Quota reading")).getByLabelText("Read every")).toBeInTheDocument();
    expect(within(region("Mode")).getByText("Cut when every watched account has less than 30 % left · every model")).toBeInTheDocument();

    const file = within(region("Set in the file"));
    expect(file.getByText("CLIProxyAPI key").nextElementSibling).toHaveTextContent("***");
    expect(file.getByText("Change these in the settings file itself.")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("laptop");
    expect(document.body.textContent).not.toContain("Operator token");
  });

  it("writes the mode at once, and drafts the rest", async () => {
    const fetch = answers(
      saved({ applied: ["routing", "low_quota.threshold_percent"] }), [200, settingsFixture()], [200, statusFixture({ low_quota: { ...statusFixture().low_quota, threshold_percent: 40 } })],
      saved({ applied: ["low_quota.rule"] }), ...rereads());
    rendered();
    await slide("PageUp");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(body(fetch, 0)).toEqual({ routing: "auto", low_quota: { threshold_percent: 40 } });
    expect(calls(fetch)).toEqual([["PUT", "/api/settings"], ["GET", "/api/settings"], ["GET", "/api/status"]]);
    expect(saveButton("Mode")).toHaveTextContent("No changes");
    expect(saveButton("Mode")).toHaveAttribute("aria-disabled", "true");

    await fireEvent.change(screen.getByLabelText("Cut when"), { target: { value: "any" } });
    typeInto("Answer budget", "900");
    expect(saveButton("Mode")).toHaveTextContent("Save 1 change");             // Cut when, the group's one drafted row
    expect(saveButton("Cutting")).toHaveTextContent("Save 1 change");
    expect(fetch).toHaveBeenCalledTimes(3);
    await save("Mode");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    expect(body(fetch, 3)).toEqual({ low_quota: { rule: "any" } });
    expect(calls(fetch).slice(3)).toEqual([["PUT", "/api/settings"], ["GET", "/api/settings"], ["GET", "/api/status"]]);
    expect(saveButton("Cutting")).toHaveTextContent("Save 1 change");     // another group's draft is its own
    expect(screen.queryByText(/^Restart .+ to apply$/)).toBeNull();
  });

  it("shows a read-only row of a writable group as text with its lock, outside the count", async () => {
    app.settings = withCliproxy({ version: "8.0.23" });
    rendered();
    const cli = within(region("CLIProxyAPI"));
    expect(cli.queryByLabelText("Version")).toBeNull();
    const version = cli.getByText("8.0.23");
    const row = version.closest(".srow") as HTMLElement;
    expect(within(row).getByText("Version")).toBeInTheDocument();
    expect(within(row).getByText("read-only")).toBeInTheDocument();
    expect(row.querySelector('[data-icon="lock"]')).not.toBeNull();
    expect(within(row).getByText(schemaRows.find((r) => r.key === "cliproxy.version")!.meaning)).toBeInTheDocument();
    expect(row.querySelector("input, select, button")).toBeNull();
    typeInto("Port", "8320");
    expect(saveButton("CLIProxyAPI")).toHaveTextContent("Save 1 change");
  });

  it("counts the watched accounts in Auto, and shows no count in the other modes", async () => {
    const ontab = vi.fn();
    app.settings = withCliproxy({ management_secret_file: SECRET });
    app.accounts = accountsFixture({ accounts: [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2", enabled: false, in_scope: false }), accountFixture({ auth_index: "c3" })] });
    const on = render(Settings, { ontab });
    const saving = within(region("Mode"));
    expect(saving.getByText("Watched accounts: 2 of 3")).toBeInTheDocument();
    // A later read that fails keeps the last count and says so, until a read answers.
    app.failed.accounts = new ApiError(0, "the core is not answering", "network");
    expect(await saving.findByText("counted at the last read; the accounts could not be read again: the core is not answering")).toBeInTheDocument();
    expect(saving.getByText("Watched accounts: 2 of 3")).toBeInTheDocument();
    answers([200, app.accounts]);
    await loadAccounts();
    await waitFor(() => expect(saving.queryByText(/could not be read again/)).toBeNull());
    await fireEvent.click(saving.getByRole("button", { name: "Manage on Accounts" }));
    expect(ontab).toHaveBeenCalledWith("accounts");
    on.unmount();

    for (const routing of ["relay", "direct"] as const) {
      app.status = statusFixture({ routing });
      const view = rendered();
      expect(within(region("Mode")).queryByText(/^Watched accounts/)).toBeNull();
      view.unmount();
    }
  });

  it("shows the panel's actions under the Web panel switch by the shell's hooks, a link in a browser", async () => {
    app.settings = withCliproxy({ url: "", port: 8317, panel: false });
    const off = rendered();
    const cli = () => within(region("CLIProxyAPI"));
    expect(cli().getByRole("switch", { name: "Web panel" })).toHaveAttribute("aria-checked", "false");
    expect(cli().getByText("Turn it on and save to open the panel; CLIProxyAPI needs a restart to apply it")).toBeInTheDocument();
    expect(cli().queryByRole("link")).toBeNull();
    off.unmount();

    app.settings = withCliproxy({ url: "", port: 8317, panel: true });
    const browser = rendered();
    const link = cli().getByRole("link", { name: /panel/i });
    expect(link).toHaveAttribute("href", `http://${location.hostname}:8317${PANEL_PATH}`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener");
    expect(cli().getByText("opens in a new tab")).toBeInTheDocument();
    expect(cli().queryByRole("button", { name: "Open the panel" })).toBeNull();
    browser.unmount();

    app.settings = withCliproxy({ url: "javascript:alert(1)", port: 8317, panel: true });
    const notWeb = rendered();
    expect(cli().queryByRole("link")).toBeNull();
    expect(cli().getByText(/javascript:alert\(1\) — not a web address/)).toBeInTheDocument();
    notWeb.unmount();

    app.settings = withCliproxy({ url: "", port: 8317, panel: true });
    window.lokki = { copyManagementSecret: vi.fn() };
    const copyOnly = rendered();
    expect(cli().getByRole("link", { name: /panel/i })).toBeInTheDocument();
    expect(cli().getByRole("button", { name: "Copy the management secret" })).toBeInTheDocument();
    copyOnly.unmount();

    window.lokki = {};                                     // a shell with no hooks shows the link
    const bare = rendered();
    expect(cli().getByRole("link", { name: /panel/i })).toHaveAttribute("href", `http://${location.hostname}:8317${PANEL_PATH}`);
    expect(cli().queryByRole("button", { name: /Open the panel|Copy the management secret/ })).toBeNull();
    bare.unmount();

    window.lokki = { openPanel: vi.fn() };
    const openOnly = rendered();
    expect(cli().queryByRole("link")).toBeNull();
    expect(cli().getByRole("button", { name: "Open the panel" })).toBeInTheDocument();
    expect(cli().queryByRole("button", { name: "Copy the management secret" })).toBeNull();
    expect(cli().queryByText(/paste it from the clipboard/)).toBeNull();
    openOnly.unmount();

    const openPanel = vi.fn().mockRejectedValue(new Error("the browser did not open"));
    const copyManagementSecret = vi.fn().mockRejectedValue(new Error("the clipboard refused"));
    window.lokki = { openPanel, copyManagementSecret };
    rendered();
    expect(cli().getByText("the panel asks for the management secret; paste it from the clipboard")).toBeInTheDocument();
    expect(cli().queryByRole("alert")).toBeNull();
    await fireEvent.click(cli().getByRole("button", { name: "Copy the management secret" }));
    await waitFor(() => expect(cli().getByRole("alert")).toHaveTextContent("could not do that: the clipboard refused"));
    expect(copyManagementSecret).toHaveBeenCalledTimes(1);
    await fireEvent.click(cli().getByRole("button", { name: "Open the panel" }));
    await waitFor(() => expect(cli().getByRole("alert")).toHaveTextContent("could not do that: the browser did not open"));
    expect(openPanel).toHaveBeenCalledTimes(1);
  });

  it("shows the versions and CLIProxyAPI's reachability in About", () => {
    app.status = statusFixture({ cliproxy: { url: "http://127.0.0.1:8318", reachable: true, version: "8.0.23" } });
    const up = rendered();
    const about = () => within(region("Updates and About"));
    expect(about().getByText("Core").nextElementSibling).toHaveTextContent("0.1.0 (revision abc1234)");
    expect(about().getByText("UI").nextElementSibling).toHaveTextContent(VERSION);
    expect(about().getByText("CLIProxyAPI").nextElementSibling).toHaveTextContent("8.0.23");
    expect(about().getByText("CLIProxyAPI running")).toBeInTheDocument();
    up.unmount();

    app.status = statusFixture({ cliproxy: { url: "http://127.0.0.1:8318", reachable: false, version: null } });
    rendered();
    expect(about().getByText("CLIProxyAPI").nextElementSibling).toHaveTextContent("not reported");
    expect(about().getByText("CLIProxyAPI not reachable")).toBeInTheDocument();
  });

  it("says the settings could not be read, in the error's words, until a read answers", async () => {
    app.settings = null;
    app.failed.settings = new ApiError(500, "settings unreadable", "other");
    const view = rendered();
    expect(screen.getByRole("alert")).toHaveTextContent("The settings could not be read: settings unreadable");
    expect(screen.queryByText("Reading the settings…")).toBeNull();
    view.unmount();

    app.failed.settings = null;
    rendered();
    expect(screen.getByText("Reading the settings…")).toBeInTheDocument();
    answers([200, settingsFixture()]);
    await loadSettings();
    await waitFor(() => expect(screen.getByLabelText("Cut when")).toBeInTheDocument());
    expect(screen.queryByText(/could not be read|Reading the settings/)).toBeNull();
  });

  it("says a later settings read failed over the last read's values, until a read answers", async () => {
    app.failed.settings = new ApiError(500, "settings unreadable", "other");
    rendered();
    expect(screen.getByRole("alert").textContent).toMatch(/^The settings could not be read again: settings unreadable; these are the values of the last read/);
    expect(screen.getByLabelText("Cut when")).toBeInTheDocument();
    answers([200, settingsFixture()]);
    await loadSettings();
    await waitFor(() => expect(screen.queryByText(/could not be read/)).toBeNull());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  // Each Advanced subgroup saves with its own button; a writable row of a group the UI does not know sits under Other.
  it("saves each Advanced subgroup with its own button, a row of an unknown group under Other", async () => {
    const knob = { ...schemaRows.find((r) => r.key === "seg_budget")!, key: "new_knob", label: "New knob", group: "tuning.someday", meaning: "a knob the UI does not know" };
    app.schema = [...schemaRows, knob];
    app.settings = settingsFixture({ new_knob: 5 } as object);
    const fetch = answers(saved({ applied: ["max_concurrent_upstream"] }), ...rereads(), saved({ applied: ["new_knob"] }), ...rereads());
    rendered();
    expect(within(region("Other")).getByLabelText("New knob")).toBeInTheDocument();
    typeInto("Streams at once", "3");
    typeInto("New knob", "7");
    expect(saveButton("Traffic")).toHaveTextContent("Save 1 change");
    expect(saveButton("Other")).toHaveTextContent("Save 1 change");
    await save("Traffic");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(body(fetch, 0)).toEqual({ max_concurrent_upstream: 3 });
    expect(saveButton("Other")).toHaveTextContent("Save 1 change");
    await save("Other");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    expect(body(fetch, 3)).toEqual({ new_knob: 7 });
  });

  it("sends listen as the one address typed", async () => {
    const fetch = answers(saved({ restart_required: ["listen"] }), ...rereads());
    rendered();
    typeInto("Address", "127.0.0.1:8382");
    await save("Connection");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3));
    expect(body(fetch, 0)).toEqual({ listen: ["127.0.0.1:8382"] });
  });

  // Review Focus 1
  it("shows the restart notice for a startup-only key and offers the restart only where the shell has one", async () => {
    answers(saved({ restart_required: ["cliproxy.port"] }), ...rereads());
    rendered();
    typeInto("Port", "8318");
    await save("CLIProxyAPI");
    expect(await screen.findByText("Restart CLIProxyAPI to apply")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Restart/ })).toBeNull();
  });

  // Review Focus 1
  it("calls the shell's restart for the service the key belongs to, and says why one failed", async () => {
    const restart = vi.fn().mockResolvedValue(undefined);
    window.lokki = { restart };
    answers(saved({ restart_required: ["cliproxy.port"] }), ...rereads());
    rendered();
    typeInto("Port", "8318");
    await save("CLIProxyAPI");
    await fireEvent.click(await screen.findByRole("button", { name: "Restart CLIProxyAPI" }));
    expect(restart).toHaveBeenCalledWith("cliproxy");
    cleanup();

    restart.mockRejectedValueOnce(new Error("the port is in use"));
    answers(saved({ restart_required: ["listen"] }), ...rereads());
    rendered();
    typeInto("Address", "127.0.0.1:8382");
    await save("Connection");
    expect(await screen.findByText("Restart the core to apply")).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Restart the core" }));
    expect(restart).toHaveBeenCalledWith("core");
    expect(await screen.findByText("could not restart the core: the port is in use")).toBeInTheDocument();
    expect(screen.getByText("Restart the core to apply")).toBeInTheDocument();
  });

  // Review Focus 2
  it("shows a 409 with its message and key beside the group and keeps what the user typed", async () => {
    const fetch = answers([409, { error: { message: "an existing cliproxy.url decides the upstream", type: "server_error" }, key: "cliproxy.port" }]);
    rendered();
    typeInto("Port", "8318");
    await save("CLIProxyAPI");
    const alert = await within(region("CLIProxyAPI")).findByRole("alert");
    expect(alert.textContent).toBe("an existing cliproxy.url decides the upstream — cliproxy.port");
    expect(alert.querySelector("code")).toHaveTextContent("cliproxy.port");
    expect(screen.getByLabelText("Port")).toHaveValue(8318);
    expect(screen.getByLabelText("Port")).toHaveAttribute("aria-invalid", "true");
    expect(saveButton("CLIProxyAPI")).toHaveTextContent("Save 1 change");
    expect(fetch).toHaveBeenCalledTimes(1);

    // A message that names its key already shows it once.
    answers([400, { error: { message: "cliproxy.port must be at most 65535", type: "invalid_request_error" }, key: "cliproxy.port" }]);
    await save("CLIProxyAPI");
    await waitFor(() => expect(within(region("CLIProxyAPI")).getByRole("alert").textContent).toBe("cliproxy.port must be at most 65535"));
    expect(within(region("CLIProxyAPI")).getByRole("alert").querySelector("code")).toBeNull();
    expect(screen.getByLabelText("Port")).toHaveAttribute("aria-invalid", "true");
  });

  it("disables a field the environment sets, and the whole form when the file is not writable", () => {
    app.settings = settingsFixture({ env_overrides: ["seg_budget"] });
    const first = rendered();
    expect(screen.getByLabelText("Answer budget")).toBeDisabled();
    expect(screen.getByLabelText("Answer budget")).toHaveAccessibleDescription(/set by the environment/);
    expect(screen.getByLabelText("Hard cap")).toBeEnabled();
    first.unmount();

    app.settings = settingsFixture({ writable: false });
    rendered();
    const controls = [...document.querySelectorAll(".groups input, .groups select, .groups [role=switch], .groups [role=radio]")];
    expect(controls.length).toBeGreaterThan(0);
    for (const control of controls) expect(control).toBeDisabled();
    for (const name of ["Mode", "Connection", "CLIProxyAPI", "Cutting"]) expect(saveButton(name)).toBeDisabled();
    expect(screen.getByText(UNWRITABLE, { selector: ".banner p" })).toBeInTheDocument();
  });

  it("gives a number input the bounds the schema states", () => {
    rendered();
    const input = screen.getByLabelText("Read every");
    expect(input).toHaveAttribute("min", "30");
    expect(input).toHaveAttribute("max", "600");
  });

  it("treats a cleared number field as no change and shows the current value as its placeholder", async () => {
    const fetch = answers();
    rendered();
    typeInto("Read every", "");
    expect(screen.getByLabelText("Read every")).toHaveAttribute("placeholder", String(schemaRows.find((r) => r.key === "low_quota.poll_seconds")?.default));
    expect(saveButton("Quota reading")).toHaveTextContent("No changes");
    await save("Quota reading");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps a pending restart notice when another key of the group is saved", async () => {
    const moved = withCliproxy({ port: 8318 });
    const fetch = answers(saved({ restart_required: ["cliproxy.port"] }), [200, moved], [200, statusFixture()],
      saved({ applied: ["cliproxy.url"] }), [200, moved], [200, statusFixture()]);
    rendered();
    typeInto("Port", "8318");
    await save("CLIProxyAPI");
    expect(await screen.findByText("Restart CLIProxyAPI to apply")).toBeInTheDocument();
    typeInto("Use a remote CLIProxyAPI", "http://127.0.0.1:9000");
    await save("CLIProxyAPI");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    expect(body(fetch, 3)).toEqual({ cliproxy: { url: "http://127.0.0.1:9000" } });
    expect(screen.getByText("Restart CLIProxyAPI to apply")).toBeInTheDocument();
  });

  it("shows a pending restart on a fresh mount and clears it once the shell has restarted the service", async () => {
    const restart = vi.fn().mockResolvedValue(undefined);
    window.lokki = { restart };
    app.restart.core = true;
    rendered();
    expect(screen.getByText("Restart the core to apply")).toBeInTheDocument();
    await fireEvent.click(screen.getByRole("button", { name: "Restart the core" }));
    await waitFor(() => expect(screen.queryByText("Restart the core to apply")).toBeNull());
    expect(app.restart.core).toBe(false);
  });

  it("keeps what was typed and says so when the save went through but the read back failed", async () => {
    answers(saved({ applied: ["low_quota.poll_seconds"] }),
      [500, { error: { message: "settings unreadable", type: "server_error" } }], [200, statusFixture()]);
    rendered();
    typeInto("Read every", "90");
    await save("Quota reading");
    const alert = await within(region("Quota reading")).findByRole("alert");
    expect(alert).toHaveTextContent("saved, but the settings could not be read back: settings unreadable");
    expect(screen.getByLabelText("Read every")).toHaveValue(90);
  });

  it("shows every address of a listen list read-only and disables the input", () => {
    app.settings = settingsFixture({ listen: ["127.0.0.1:8371", "127.0.0.1:8372"] });
    rendered();
    const input = screen.getByLabelText("Address");
    expect(input).toBeDisabled();
    expect(input).toHaveValue("127.0.0.1:8371, 127.0.0.1:8372");
    expect(input).toHaveAccessibleDescription(/edit the file to change several addresses/);
  });

  const nav = () => within(screen.getByRole("navigation", { name: "Settings groups" }));
  const advanced = () => region("Advanced").querySelector("details") as HTMLDetailsElement;
  const named = (accounts: string[]) => settingsFixture({ low_quota: { ...settingsFixture().low_quota, accounts }, cliproxy: { ...settingsFixture().cliproxy, management_secret_file: SECRET } });

  it("shows Mode, Connection and Updates and About open, and Advanced closed by default", () => {
    rendered();
    for (const open of ["Mode", "Connection", "Updates and About"]) expect(region(open).querySelector("details"), open).toBeNull();
    expect(advanced()).not.toHaveAttribute("open");
    expect(within(region("Advanced")).getByRole("heading", { name: "Advanced" })).toBeInTheDocument();
    expect(nav().getAllByRole("link").map((a) => a.textContent?.trim())).toEqual(["Mode", "Connection", "Updates and About", "Advanced"]);
  });

  // Review Focus 12, with the slider at an end
  it("shows Auto's rule rows only while Auto is the mode, and leaving Auto writes nothing", async () => {
    app.settings = withCliproxy({ management_secret_file: SECRET });
    app.accounts = accountsFixture({ accounts: [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2" })] });
    const fetch = answers(
      saved({ applied: ["routing"] }), [200, settingsFixture()], [200, statusFixture({ routing: "relay" })],
      saved({ applied: ["low_quota.rule"] }), [200, settingsFixture({ low_quota: { ...settingsFixture().low_quota, rule: "any" } })], [200, statusFixture()]);
    rendered();
    const mode = () => within(region("Mode"));
    const rules = () => [mode().queryByText(/^Watched accounts/), mode().queryByLabelText("Cut when"), mode().queryByText("Models cut when low")];
    expect(rules().every((el) => el !== null)).toBe(true);
    await fireEvent.change(screen.getByLabelText("Cut when"), { target: { value: "any" } });
    expect(saveButton("Mode")).toHaveTextContent("Save 1 change");

    // The slider to its right end: one PUT, the mode alone; the rows go, and so does their draft from the count.
    await slide("End");
    await waitFor(() => expect(rules().every((el) => el === null)).toBe(true));
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(body(fetch, 0)).toEqual({ routing: "relay" });
    expect(mode().queryByRole("button", { name: "Manage on Accounts" })).toBeNull();
    expect(saveButton("Mode")).toHaveTextContent("No changes");
    await save("Mode");
    expect(fetch).toHaveBeenCalledTimes(3);                              // a Save with the rows hidden sends nothing

    // Auto again, as the top bar or the tray would choose it: the draft shows again, and the group's Save sends it.
    app.status = statusFixture();
    await waitFor(() => expect(mode().getByLabelText("Cut when")).toHaveValue("any"));
    expect(saveButton("Mode")).toHaveTextContent("Save 1 change");
    await save("Mode");
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6));
    expect(body(fetch, 3)).toEqual({ low_quota: { rule: "any" } });
  });

  it("orders Auto's rows as spec 6.1 does: Watched accounts, Cut when, Models cut when low", () => {
    rendered();
    const mode = region("Mode");
    const rows = [within(mode).getByText(/^Watched accounts/), within(mode).getByText("Cut when"), within(mode).getByText("Models cut when low")];
    rows.slice(1).forEach((el, i) => {
      expect(rows[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING, el.textContent ?? "").toBeTruthy();
    });
  });

  it("draws the Mode group as the mode slider, with Auto's rows only between its ends", async () => {
    app.settings = withCliproxy({ management_secret_file: SECRET });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    for (const [routing, value] of [["direct", "0"], ["auto", "30"], ["relay", "100"]] as const) {
      app.status = statusFixture({ routing });
      const view = rendered();
      const mode = within(region("Mode"));
      expect(slider(), routing).toHaveValue(value);
      expect(mode.queryByLabelText("Cut below, % left"), routing).toBeNull();
      expect(mode.queryAllByRole("radio", { name: /^(Direct|Auto|Relay)/ }), routing).toHaveLength(0);   // the top bar's, not the group's
      const shown = routing === "auto";
      expect(mode.queryByText("Auto's rule") !== null, routing).toBe(shown);
      expect(mode.queryByLabelText("Cut when") !== null, routing).toBe(shown);
      expect(mode.queryByText(/^Watched accounts/) !== null, routing).toBe(shown);
      expect(mode.queryByText("Models cut when low") !== null, routing).toBe(shown);
      expect(mode.getByText(MODES.find((m) => m.mode === routing)!.mode === "auto"
        ? "Cut when every watched account has less than 30 % left · every model" : MODES.find((m) => m.mode === routing)!.sentence)).toBeInTheDocument();
      view.unmount();
    }
    expect(fetch).not.toHaveBeenCalled();

    app.status = null;                                                  // drawn before a status is read
    rendered();
    expect(slider()).toBeDisabled();
    expect(slider()).not.toHaveAttribute("aria-valuetext");
  });

  // A core for the models picker: the list `getModels` answers (or the failure), the models the file holds,
  // and a PUT that waits on `release` so a test can press while it runs.
  function modelsCore(over: { list?: string[]; failure?: [number, string]; models?: string[]; hold?: boolean } = {}) {
    const core = { models: over.models ?? [], list: over.list ?? ["m1", "m2", "m3"], failure: over.failure ?? null, puts: [] as unknown[], reads: 0, release: () => {} };
    const json = (status: number, value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } }));
    const gate = over.hold ? new Promise<void>((resolve) => { core.release = resolve; }) : Promise.resolve();
    vi.mocked(getModels).mockImplementation(async () => {
      core.reads++;
      if (core.failure) throw new ApiError(core.failure[0], core.failure[1], "other");
      return { models: core.list };
    });
    const fetch = vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const sent = JSON.parse(String(init.body));
        core.puts.push(sent);
        await gate;
        core.models = sent.low_quota.models;
        return json(200, { applied: ["low_quota.models"], restart_required: [], comments_dropped: false });
      }
      if (path === "/api/settings") return json(200, settingsFixture());
      if (path === "/api/status") return json(200, statusFixture({ low_quota: { ...statusFixture().low_quota, models: core.models } }));
      return json(404, null);
    });
    vi.stubGlobal("fetch", fetch);
    app.status = statusFixture({ low_quota: { ...statusFixture().low_quota, models: core.models } });
    return { ...core, fetch, state: core };
  }
  const picker = () => within(region("Mode"));
  const MODEL_NOTE = "Nothing is picked yet, so every model is still cut when low";

  it("picks the models cut when low with one PUT per change, busy while it runs", async () => {
    const core = modelsCore({ hold: true });
    rendered();
    expect(picker().getByText("Models cut when low")).toBeInTheDocument();
    await fireEvent.click(picker().getByRole("radio", { name: "Only the ones I pick" }));
    const m1 = await picker().findByRole("checkbox", { name: "m1" });
    expect(picker().getAllByRole("checkbox").map((el) => (el as HTMLInputElement).checked)).toEqual([false, false, false]);
    expect(core.state.puts).toEqual([]);

    m1.focus();
    await fireEvent.click(m1);
    await waitFor(() => expect(core.state.puts).toEqual([{ low_quota: { models: ["m1"] } }]));
    const group = picker().getByRole("radiogroup", { name: "Models cut when low" });
    expect(group).toHaveAttribute("aria-busy", "true");
    const row = picker().getByText("Models cut when low").closest(".srow") as HTMLElement;
    expect(row.querySelector('[data-icon="spinner"]')).not.toBeNull();          // the busy mark, so a tick never looks lost
    expect(m1).toBeEnabled();
    expect(m1).toHaveFocus();
    await fireEvent.click(picker().getByRole("checkbox", { name: "m2" }));        // a press meanwhile writes nothing
    expect(core.state.puts).toHaveLength(1);
    expect(picker().getByRole("checkbox", { name: "m2" })).not.toBeChecked();

    core.state.release();
    await waitFor(() => expect(group).not.toHaveAttribute("aria-busy"));
    expect(row.querySelector('[data-icon="spinner"]')).toBeNull();
    expect(picker().getByRole("checkbox", { name: "m1" })).toBeChecked();
    expect(picker().getByRole("checkbox", { name: "m1" })).toHaveFocus();
    expect(calls(core.fetch).filter(([method]) => method === "PUT")).toHaveLength(1);
    expect(calls(core.fetch).slice(-2)).toEqual([["GET", "/api/settings"], ["GET", "/api/status"]]);

    await fireEvent.click(picker().getByRole("checkbox", { name: "m3" }));
    await waitFor(() => expect(core.state.puts).toHaveLength(2));
    expect(core.state.puts[1]).toEqual({ low_quota: { models: ["m1", "m3"] } });
    await waitFor(() => expect(picker().getByRole("checkbox", { name: "m3" })).toBeChecked());
    await fireEvent.click(picker().getByRole("checkbox", { name: "m1" }));
    await waitFor(() => expect(core.state.puts).toHaveLength(3));
    expect(core.state.puts[2]).toEqual({ low_quota: { models: ["m3"] } });

    await waitFor(() => expect(picker().getByRole("checkbox", { name: "m1" })).not.toBeChecked());
    await fireEvent.click(picker().getByRole("radio", { name: "Every model" }));
    await waitFor(() => expect(core.state.puts).toHaveLength(4));
    expect(core.state.puts[3]).toEqual({ low_quota: { models: [] } });
    await waitFor(() => expect(picker().queryByRole("checkbox", { name: "m1" })).toBeNull());
    expect(core.state.reads).toBe(1);                                                // read on entering, not after a write
  });

  it("says what each choice means: every model is cut, or a model not picked passes, at any thinking level", async () => {
    const LEVEL = "A model at any thinking level, such as gpt-5.5(high), counts as gpt-5.5.";
    modelsCore();
    rendered();
    expect(picker().getByText("Every model is cut when the quota is low.")).toBeInTheDocument();
    expect(picker().queryByText(/always passes through at full price/)).toBeNull();
    expect(picker().queryByText(LEVEL)).toBeNull();
    await fireEvent.click(picker().getByRole("radio", { name: "Only the ones I pick" }));
    expect(picker().getByText("A model not picked always passes through at full price")).toBeInTheDocument();
    expect(picker().getByText(LEVEL)).toBeInTheDocument();
    expect(picker().queryByText("Every model is cut when the quota is low.")).toBeNull();
  });

  it("keeps Only the ones I pick local until a model is picked, and says every model is still cut", async () => {
    const core = modelsCore();
    rendered();
    expect(picker().queryByText(MODEL_NOTE)).toBeNull();
    expect(picker().getByRole("radio", { name: "Every model" })).toBeChecked();
    await fireEvent.click(picker().getByRole("radio", { name: "Only the ones I pick" }));
    expect(await picker().findByText(MODEL_NOTE)).toBeInTheDocument();
    expect(picker().getByRole("radio", { name: "Only the ones I pick" })).toBeChecked();
    await fireEvent.click(picker().getByRole("radio", { name: "Every model" }));            // back to the default writes nothing
    expect(picker().queryByText(MODEL_NOTE)).toBeNull();
    await fireEvent.click(picker().getByRole("radio", { name: "Only the ones I pick" }));
    await fireEvent.click(await picker().findByRole("checkbox", { name: "m2" }));
    await waitFor(() => expect(core.state.puts).toEqual([{ low_quota: { models: ["m2"] } }]));
    await waitFor(() => expect(picker().queryByText(MODEL_NOTE)).toBeNull());
    // Unpicking the last one leaves the picker open, with its note, and writes the empty list.
    await fireEvent.click(await picker().findByRole("checkbox", { name: "m2" }));
    await waitFor(() => expect(core.state.puts).toHaveLength(2));
    expect(core.state.puts[1]).toEqual({ low_quota: { models: [] } });
    expect(await picker().findByText(MODEL_NOTE)).toBeInTheDocument();
    expect(picker().getByRole("radio", { name: "Only the ones I pick" })).toBeChecked();
  });

  it("marks a chosen model the list lacks, with its Remove", async () => {
    const core = modelsCore({ models: ["m1", "gone"], list: ["m1", "m2"] });
    rendered();
    expect(await picker().findByRole("checkbox", { name: "m1" })).toBeChecked();
    expect(picker().getByRole("radio", { name: "Only the ones I pick" })).toBeChecked();
    expect(picker().getByText("Chosen but not listed:")).toBeInTheDocument();
    expect(picker().getByText("gone")).toBeInTheDocument();
    expect(picker().queryByRole("checkbox", { name: "gone" })).toBeNull();
    expect(picker().queryByText(/Chosen but not listed: m1/)).toBeNull();
    await fireEvent.click(picker().getByRole("button", { name: "Remove gone from the models cut when low" }));
    await waitFor(() => expect(core.state.puts).toEqual([{ low_quota: { models: ["m1"] } }]));
    await waitFor(() => expect(picker().queryByText("Chosen but not listed:")).toBeNull());
    expect(picker().getByRole("checkbox", { name: "m1" })).toBeChecked();
  });

  it("says why the model list could not be read, offers Retry, and keeps every chosen name removable", async () => {
    const core = modelsCore({ models: ["m1", "gone"], failure: [502, "CLIProxyAPI did not answer"] });
    rendered();
    const alert = await picker().findByText("lokki could not read the models CLIProxyAPI offers: CLIProxyAPI did not answer");
    expect(alert.closest('[role="alert"]')).not.toBeNull();
    expect(picker().queryByRole("checkbox")).toBeNull();
    expect(picker().queryByText(/Chosen but not listed/)).toBeNull();               // no list to compare against
    for (const name of ["m1", "gone"]) {
      expect(picker().getByText(name)).toBeInTheDocument();
      expect(picker().getByRole("button", { name: `Remove ${name} from the models cut when low` })).toBeEnabled();
    }
    await fireEvent.click(picker().getByRole("button", { name: "Remove gone from the models cut when low" }));
    await waitFor(() => expect(core.state.puts).toEqual([{ low_quota: { models: ["m1"] } }]));
    await waitFor(() => expect(picker().queryByText("gone")).toBeNull());

    core.state.failure = null;
    core.state.list = ["m1", "m2"];
    await fireEvent.click(picker().getByRole("button", { name: "Retry" }));
    expect(await picker().findByRole("checkbox", { name: "m1" })).toBeChecked();
    expect(picker().queryByText(/could not read the models/)).toBeNull();
    expect(core.state.reads).toBe(2);
  });

  it("shows the models only while Auto is the mode", async () => {
    const core = modelsCore({ models: ["m1"] });
    for (const routing of ["relay", "direct"] as const) {
      app.status = statusFixture({ routing, low_quota: { ...statusFixture().low_quota, models: ["m1"] } });
      const view = rendered();
      expect(within(region("Mode")).queryByText("Models cut when low"), routing).toBeNull();
      expect(within(region("Mode")).queryByRole("radio", { name: "Only the ones I pick" }), routing).toBeNull();
      view.unmount();
    }
    expect(core.state.reads).toBe(0);                                              // nothing reads the list outside Auto
    app.status = statusFixture({ low_quota: { ...statusFixture().low_quota, models: ["m1"] } });
    rendered();
    expect(await picker().findByRole("checkbox", { name: "m1" })).toBeChecked();
    expect(core.state.reads).toBe(1);
    // Leaving Auto takes the rows away and writes nothing; the list stays chosen in the file.
    app.status = statusFixture({ routing: "relay", low_quota: { ...statusFixture().low_quota, models: ["m1"] } });
    await waitFor(() => expect(picker().queryByText("Models cut when low")).toBeNull());
    expect(core.state.puts).toEqual([]);
  });

  it("offers the picker on a core with no management secret file, and says nothing of reading the quota", async () => {
    const core = modelsCore();
    app.settings = withCliproxy({ management_secret_file: "" });
    rendered();
    await fireEvent.click(picker().getByRole("radio", { name: "Only the ones I pick" }));
    const m1 = await picker().findByRole("checkbox", { name: "m1" });
    // The row and the list say what CLIProxyAPI offers; the Mode card's own no-secret sentence is not theirs.
    const row = picker().getByText("Models cut when low").closest(".srow") as HTMLElement;
    const list = m1.closest(".mpick") as HTMLElement;
    expect(row.textContent).not.toMatch(/quota|read/i);
    expect(list.textContent).not.toMatch(/quota|read/i);
    expect(within(list).getByText(MODEL_NOTE)).toBeInTheDocument();
    await fireEvent.click(m1);
    await waitFor(() => expect(core.state.puts).toEqual([{ low_quota: { models: ["m1"] } }]));
  });

  it("counts only the named accounts the core watches, as Accounts does", () => {
    app.settings = settingsFixture({ low_quota: { ...settingsFixture().low_quota, accounts: ["a1", "b2", "gone"] }, cliproxy: { ...settingsFixture().cliproxy, management_secret_file: SECRET } });
    app.accounts = accountsFixture({ accounts: [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2" })] });
    rendered();
    expect(within(region("Mode")).getByText("Watched accounts: 2 of 2")).toBeInTheDocument();
  });

  it("shows Cut when only with more than one watched account, or while the count is unread", () => {
    const rows = [accountFixture({ auth_index: "a1" }), accountFixture({ auth_index: "b2" }), accountFixture({ auth_index: "c3", enabled: false, in_scope: false })];
    const cases: Array<[string, string[], typeof app.accounts, boolean]> = [
      ["none named, none read", [], null, true],
      ["none named, two in scope", [], accountsFixture({ accounts: rows }), true],
      ["none named, one in scope", [], accountsFixture({ accounts: rows.slice(1, 3) }), false],
      ["one named present, two others not watched", ["a1"], accountsFixture({ accounts: [rows[0], { ...rows[1], in_scope: false }, rows[2]] }), false],
      ["two named, none read", ["a1", "b2"], null, true],
    ];
    for (const [label, accounts, read, shown] of cases) {
      app.settings = named(accounts);
      app.accounts = read;
      const view = rendered();
      expect(within(region("Mode")).queryByLabelText("Cut when") !== null, label).toBe(shown);
      view.unmount();
    }
  });

  it("shows the Watched row as its label alone on a core with no management secret file, Auto's rows still editable", () => {
    for (const read of [null, accountsFixture({ read_at: null, accounts: [] })]) {
      app.settings = withCliproxy({ management_secret_file: "" });
      app.accounts = read;
      const view = rendered();
      const mode = within(region("Mode"));
      expect(mode.getByText("Watched accounts")).toBeInTheDocument();
      expect(mode.queryByText(/No account read yet|Reading the accounts|Watched accounts: /)).toBeNull();
      expect(slider()).toBeEnabled();
      expect(mode.getByLabelText("Cut when")).toBeEnabled();
      view.unmount();
    }
  });

  it("says the no-secret line with Auto's clause between the ends", () => {
    for (const routing of ["auto", "relay", "direct"] as const) {
      app.status = statusFixture({ routing });
      app.settings = withCliproxy({ management_secret_file: "" });
      const bare = rendered();
      expect(within(region("Mode")).queryByText(NO_SECRET + NO_SECRET_AUTO) !== null, routing).toBe(routing === "auto");
      expect(within(region("Mode")).queryByText(NO_SECRET), routing).toBeNull();
      bare.unmount();

      app.settings = withCliproxy({ management_secret_file: "/data/secrets/cliproxy-mgmt.secret" });
      const set = rendered();
      expect(within(region("Mode")).queryByText(/^lokki does not read the quota here/), routing).toBeNull();
      set.unmount();
    }
  });

  it("reads the update line from app.update with Check now, and shows none in a plain browser", async () => {
    const about = () => within(region("Updates and About"));
    const plain = rendered();
    expect(about().queryByRole("button", { name: "Check now" })).toBeNull();
    expect(about().queryByText(/lokki checks once a day/)).toBeNull();
    plain.unmount();

    let answer: (u: NonNullable<typeof app.update>) => void = () => {};
    window.lokki = { checkForUpdate: vi.fn(() => new Promise<NonNullable<typeof app.update>>((done) => { answer = done; })) };
    const update = { state: "up_to_date" as const, current_version: "0.1.0", version: null, notes_url: null, can_install: false, reason: null, error: null };
    app.update = update;
    rendered();
    expect(about().getByText(updateLine(update))).toBeInTheDocument();
    expect(about().getByText("lokki checks once a day; a new release shows a banner at the top")).toBeInTheDocument();
    const check = about().getByRole("button", { name: "Check now" });
    await fireEvent.click(check);
    expect(window.lokki.checkForUpdate).toHaveBeenCalledTimes(1);
    expect(check).toHaveAttribute("aria-busy", "true");
    expect(about().getByText("Checking for an update…")).toBeInTheDocument();
    await fireEvent.click(check);
    expect(window.lokki.checkForUpdate).toHaveBeenCalledTimes(1);          // a press meanwhile does nothing
    answer({ ...update, state: "available", version: "0.2.0", can_install: true });
    await waitFor(() => expect(about().getByText("lokki 0.2.0 is available.")).toBeInTheDocument());
    expect(check).not.toHaveAttribute("aria-busy", "true");
    expect(check).toBeEnabled();
  });

  // The banner's states: Check now where it shows it; busy, with no second call, while a check runs; none while a download or install runs.
  it.each([
    ["idle", { state: "idle" as const }, true],
    ["up to date", { state: "up_to_date" as const }, true],
    ["available", { state: "available" as const, version: "0.2.0", can_install: true }, true],
    ["a failed install", { state: "error" as const, version: "0.2.0", error: "the core (pid 4242) is still alive" }, true],
    ["a failed check", { state: "error" as const, error: "the update check failed: 404" }, true],
    ["checking", { state: "checking" as const }, true],
    ["downloading", { state: "downloading" as const, version: "0.2.0" }, false],
    ["installing", { state: "installing" as const, version: "0.2.0" }, false],
  ])("offers Check now as the banner does while the update is %s", async (_state, over, shown) => {
    const checkForUpdate = vi.fn(() => new Promise<never>(() => {}));
    window.lokki = { checkForUpdate };
    app.update = { current_version: "0.1.0", version: null, notes_url: null, can_install: false, reason: null, error: null, ...over };
    rendered();
    const button = within(region("Updates and About")).queryByRole("button", { name: "Check now" });
    expect(button !== null).toBe(shown);
    if (!button) return;
    if (over.state === "checking") {
      expect(button).toHaveAttribute("aria-busy", "true");
      await fireEvent.click(button);
      expect(checkForUpdate).not.toHaveBeenCalled();
    } else {
      expect(button).not.toHaveAttribute("aria-busy");
      await fireEvent.click(button);
      expect(checkForUpdate).toHaveBeenCalledTimes(1);
    }
  });

  it("says an update error through updateWords, as the banner does", () => {
    app.update = { state: "error", current_version: "0.1.0", version: null, notes_url: null, can_install: false, reason: null, error: "the update check failed: 404" };
    rendered();
    expect(within(region("Updates and About")).getByText(updateWords(app.update).error!)).toBeInTheDocument();
  });

  it("opens Advanced when a link to a group inside it is followed, and counts its unsaved changes on its link", async () => {
    rendered();
    expect(advanced()).not.toHaveAttribute("open");
    expect(nav().getByRole("link", { name: "Advanced" })).toHaveAttribute("href", "#card-advanced");
    await fireEvent.click(nav().getByRole("link", { name: "Advanced" }));
    expect(advanced().open).toBe(true);
    advanced().open = false;
    location.hash = "#card-cliproxy";                                      // a link to a group inside the fold
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await waitFor(() => expect(advanced().open).toBe(true));
    location.hash = "";

    expect(nav().getByRole("link", { name: "Advanced" })).toHaveAccessibleName("Advanced");
    typeInto("Port", "8320");
    typeInto("Answer budget", "900");
    await fireEvent.change(screen.getByLabelText("Cut when"), { target: { value: "any" } });     // a Mode draft is not Advanced's
    expect(nav().getByRole("link", { name: "Advanced, 2 unsaved changes" })).toBeInTheDocument();
    typeInto("Port", String(settingsFixture().cliproxy.port));
    expect(nav().getByRole("link", { name: "Advanced, 1 unsaved change" })).toBeInTheDocument();
  });

  // Review Focus 12's neighbour: the bar is above the groups, outside the fold, so closing or scrolling it cannot hide it.
  it("keeps the restart bar in view for a key saved inside Advanced", async () => {
    answers(saved({ restart_required: ["cliproxy.port"] }), ...rereads());
    rendered();
    await fireEvent.click(nav().getByRole("link", { name: "Advanced" }));
    typeInto("Port", "8318");
    await save("CLIProxyAPI");
    const bar = (await screen.findByText("Restart CLIProxyAPI to apply")).closest(".restart")!;
    expect(bar.closest("details")).toBeNull();                              // not inside the fold
    const groups = document.querySelector(".groups")!;
    expect(bar.parentElement).toBe(groups);
    expect(groups.firstElementChild).toBe(bar);                             // above every group
    advanced().open = false;
    expect(screen.getByText("Restart CLIProxyAPI to apply")).toBeVisible();
  });

  describe("while a write or a hook runs", () => {
    // A PUT held until `release`; every read answers at once with what the core would report after it.
    function gated(after: { status?: object; settings?: object } = {}) {
      let release: () => void = () => {};
      const held = new Promise<void>((done) => { release = done; });
      const json = (v: unknown) => new Response(JSON.stringify(v), { status: 200, headers: { "content-type": "application/json" } });
      const fetch = vi.fn(async (path: string, init?: RequestInit) => {
        if (init?.method === "PUT") { await held; return json({ applied: [], restart_required: [], comments_dropped: false }); }
        if (path === "/api/settings") return json(after.settings ?? settingsFixture());
        if (path === "/api/accounts") return json(accountsFixture());
        return json(after.status ?? statusFixture());
      });
      vi.stubGlobal("fetch", fetch);
      return { count: () => fetch.mock.calls.filter((c) => c[1]?.method === "PUT").length, release: () => release() };
    }
    // A shell hook held until `release`, which settles it as the row says.
    function hooked(name: "restart" | "openPanel" | "copyManagementSecret", settle: "resolve" | "reject") {
      let release: () => void = () => {};
      const fn = vi.fn(() => new Promise<void>((done, fail) => { release = () => (settle === "resolve" ? done() : fail(new Error("the shell said no"))); }));
      window.lokki = { [name]: fn };
      return { count: () => fn.mock.calls.length, release: () => release() };
    }
    const panelOn = () => { app.settings = withCliproxy({ panel: true }); };
    // A settings read held until `release`, which answers it with another failure.
    function failingRead() {
      app.settings = null;
      app.failed.settings = new ApiError(500, "settings unreadable", "other");
      let release: () => void = () => {};
      const held = new Promise<void>((done) => { release = done; });
      const fetch = vi.fn(async () => {
        await held;
        return new Response(JSON.stringify({ error: { message: "still unreadable" } }), { status: 500 });
      });
      vi.stubGlobal("fetch", fetch);
      return { count: () => fetch.mock.calls.length, release: () => release() };
    }

    // Each row: what to set up before the page renders, what to type once it has, the control pressed
    // twice, the element that says busy (the control itself when null), and what shows once it answered.
    it.each([
      ["a group's Save button", () => gated({ settings: settingsFixture({ low_quota: { ...settingsFixture().low_quota, rule: "any" } }) }),
        () => fireEvent.change(screen.getByLabelText("Cut when"), { target: { value: "any" } }),
        () => saveButton("Mode"), null,
        (el: HTMLElement) => expect(el).toHaveTextContent("No changes")],
      ["a restart button", () => { app.restart.cliproxy = true; return hooked("restart", "reject"); }, null,
        () => screen.getByRole("button", { name: /^Restart CLIProxyAPI$|^Restarting…$/ }), null,
        () => expect(screen.getByRole("alert")).toHaveTextContent("could not restart CLIProxyAPI: the shell said no")],
      ["Open the panel", () => { panelOn(); return hooked("openPanel", "resolve"); }, null,
        () => screen.getByRole("button", { name: "Open the panel" }), null, () => {}],
      ["Copy the management secret", () => { panelOn(); return hooked("copyManagementSecret", "resolve"); }, null,
        () => screen.getByRole("button", { name: /^(Copy the management secret|Copied)$/ }), null,
        (el: HTMLElement) => expect(el).toHaveTextContent("Copied")],
      ["Retry after a failed read", failingRead, null,
        () => screen.getByRole("button", { name: "Retry" }), null,
        () => expect(screen.getByRole("alert")).toHaveTextContent("The settings could not be read: still unreadable")],
    ] as const)("keeps %s enabled and busy, ignores a press meanwhile, and keeps the focus", async (_name, setup, prepare, control, busyOf, done) => {
      const pending = setup();
      rendered();
      await prepare?.();
      const el = control();
      const busy = () => (busyOf ?? control)();
      el.focus();
      await fireEvent.click(el);
      await waitFor(() => expect(busy()).toHaveAttribute("aria-busy", "true"));
      expect(el).toBeEnabled();
      expect(el).not.toHaveAttribute("aria-disabled", "true");
      await fireEvent.click(el);
      expect(pending.count()).toBe(1);
      pending.release();
      await waitFor(() => expect(busy()).not.toHaveAttribute("aria-busy", "true"));
      done(el);
      expect(control()).toBe(el);
      expect(document.activeElement).toBe(el);
      expect(pending.count()).toBe(1);
    });
  });
});
