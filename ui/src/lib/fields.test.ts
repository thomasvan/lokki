import { describe, expect, it } from "vitest";
import { schemaRows, settingsFixture } from "../test-helpers";
import type { SchemaRow } from "./api";
import { GROUP_TITLES, PORT, group, patchOf, valueOf, widget } from "./fields";

const row = (key: string) => {
  const found = schemaRows.find((r) => r.key === key);
  if (!found) throw new Error(`no schema row ${key}`);
  return found;
};
const keys = (rows: Array<{ key: string }>) => rows.map((r) => r.key);
const shown = (g: ReturnType<typeof group>) =>
  [...g.saving, ...g.connection, ...g.address, ...g.cliproxy, ...Object.values(g.tuning).flat(), ...g.file];

describe("GROUP_TITLES", () => {
  it("lists the groups in Settings' order, the Advanced subgroups last", () => {
    expect(Object.entries(GROUP_TITLES).map(([id, g]) => [id, g.title])).toEqual([
      ["saving", "Mode"], ["connection", "Connection"], ["about", "Updates and About"], ["advanced", "Advanced"],
      ["address", "The core's address"], ["cliproxy", "CLIProxyAPI"], ["tuning.quota_reading", "Quota reading"], ["tuning.cutting", "Cutting"],
      ["tuning.tool_calls", "Tool calls"], ["tuning.traffic", "Traffic"], ["tuning.other", "Other"], ["file", "Set in the file"],
    ]);
  });
});

describe("group", () => {
  const groups = group(schemaRows);

  // The UI cannot import schema.GROUPS; this is what holds the core's groups and the UI's titles together.
  it("puts every fixture row in a known group", () => {
    expect(groups.tuning.other).toEqual([]);
    expect(groups.file.every((r) => r.group === "file")).toBe(true);
    for (const r of schemaRows.filter((r) => r.group !== "hidden")) expect(Object.keys(GROUP_TITLES)).toContain(r.group);
    expect(keys(groups.saving)).toEqual(["routing", "low_quota.threshold_percent", "low_quota.rule", "low_quota.accounts", "low_quota.models"]);
    expect(keys(groups.connection)).toEqual(["port"]);
    expect(keys(groups.address)).toEqual(["listen"]);
    expect(keys(groups.cliproxy)).toEqual(["cliproxy.port", "cliproxy.url", "cliproxy.panel", "cliproxy.version"]);
    expect(keys(groups.tuning.quota_reading)).toEqual(["low_quota.poll_seconds"]);
    expect(keys(groups.tuning.tool_calls)).toEqual(["text_calls", "text_calls_pad"]);
    // Every row the schema shows, once, and the Port of the UI's own.
    expect(keys(shown(groups)).sort()).toEqual([...keys(schemaRows.filter((r) => r.group !== "hidden")), PORT].sort());
  });

  // Review Focus 7
  it("keeps a row of an unknown group: writable under Advanced › Other, read-only under Advanced › Set in the file", () => {
    const base = row("seg_budget");
    const extra: SchemaRow[] = [
      { ...base, key: "new_knob", group: "tuning.someday", writable: true },
      { ...base, key: "new_fact", group: "elsewhere", writable: false },
    ];
    const g = group([...schemaRows, ...extra]);
    expect(keys(g.tuning.other)).toEqual(["new_knob"]);
    expect(keys(g.file)).toContain("new_fact");
  });

  it("never shows a hidden row", () => {
    expect(keys(shown(groups)).filter((k) => k.startsWith("auth."))).toEqual([]);
    expect(keys(groups.file)).toContain("cli_proxy_api_key");
  });
});

describe("widget", () => {
  it("picks the input by the row's type", () => {
    expect(widget(row("text_calls"))).toBe("checkbox");
    expect(widget(row("seg_budget"))).toBe("number");
    expect(widget(row("pacing_seconds"))).toBe("number");   // a float
    expect(widget(row("routing"))).toBe("select");
    expect(widget(row("cliproxy.url"))).toBe("text");
    expect(widget(row("listen"))).toBe("text");              // one address, sent as a list
  });
});

describe("patchOf", () => {
  it("merges keys under one prefix into one nested object", () => {
    expect(patchOf([["low_quota.threshold_percent", 25], ["low_quota.rule", "any"]]))
      .toEqual({ low_quota: { threshold_percent: 25, rule: "any" } });
  });

  it("sends listen as a list of its one address", () => {
    expect(patchOf([["listen", "127.0.0.1:8382"]])).toEqual({ listen: ["127.0.0.1:8382"] });
  });

  it("sends the Port as the address the settings hold, on its host", () => {
    for (const [address, sent] of [["127.0.0.1:8371", "127.0.0.1:9476"], ["localhost:8371", "localhost:9476"], ["[::1]:8371", "[::1]:9476"]]) {
      expect(patchOf([[PORT, 9476]], settingsFixture({ listen: [address] }))).toEqual({ listen: [sent] });
    }
  });

  it("keeps an empty string, which clears cliproxy.url", () => {
    expect(patchOf([["cliproxy.url", ""], ["cliproxy.port", 8318]])).toEqual({ cliproxy: { url: "", port: 8318 } });
  });

  it("keeps top-level and nested keys side by side", () => {
    expect(patchOf([["routing", "relay"], ["seg_budget", 900], ["cliproxy.port", 8318]]))
      .toEqual({ routing: "relay", seg_budget: 900, cliproxy: { port: 8318 } });
  });
});

describe("valueOf", () => {
  const view = settingsFixture({ routing: "relay" });

  it("reads a nested key and a top-level key", () => {
    expect(valueOf(view, "low_quota.threshold_percent")).toBe(row("low_quota.threshold_percent").default);
    expect(valueOf(view, "routing")).toBe("relay");
    expect(valueOf(view, "cliproxy.url")).toBe("");
  });

  it("reads listen as its first address, and the Port as that address's port", () => {
    expect(valueOf(view, "listen")).toBe((row("listen").default as string[])[0]);
    expect(valueOf(settingsFixture({ listen: ["localhost:9477"] }), PORT)).toBe(9477);
  });
});
