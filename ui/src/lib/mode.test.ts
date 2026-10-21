import { describe, expect, it } from "vitest";
import { accountFixture, accountsFixture, schemaRows, statusFixture } from "../test-helpers";
import { MODES, NO_SECRET, NO_SECRET_AUTO, READ_FAILED_AUTO, UNWATCHED_MODES, modeLabel, position, positionBody, positionText, positionWords, ruleLine, watchedCount } from "./mode";

describe("mode", () => {
  // The labels have two homes on purpose: the schema's for Settings and the tray, MODES' for the control.
  it("names the modes as the schema does", () => {
    const routing = schemaRows.find((r) => r.key === "routing")!;
    expect(MODES.map((m) => m.mode)).toEqual(["direct", "auto", "relay"]);     // the slider's order, left to right
    expect([...routing.choices].sort()).toEqual(MODES.map((m) => m.mode).sort());
    expect(MODES.map((m) => m.label)).toEqual(MODES.map((m) => routing.choice_labels[m.mode]));
  });

  it("counts the watched accounts", () => {
    const row = (id: string, over = {}) => accountFixture({ auth_index: id, ...over });
    const read = (...rows: ReturnType<typeof row>[]) => accountsFixture({ accounts: rows });
    // Two named, both present: the core marks them in_scope. A third row it does not watch is not counted.
    expect(watchedCount(read(row("a1"), row("b2"), row("c3", { in_scope: false })))).toBe(2);
    // Three named, one of them among no row: the core cannot watch it, so no row says it is, and it is not counted.
    expect(watchedCount(read(row("a1"), row("b2")))).toBe(2);
    // An empty scope: the rows the core marks in scope, enabled or not as it says.
    expect(watchedCount(read(row("a1"), row("b2"), row("c3", { enabled: false, in_scope: false })))).toBe(2);
    // No read yet, or the accounts not loaded.
    expect(watchedCount(read())).toBe(0);
    expect(watchedCount(accountsFixture({ read_at: null, accounts: [] }))).toBeNull();
    expect(watchedCount(null)).toBeNull();
  });

  const rule = (over: Partial<{ threshold_percent: number; rule: "all" | "any"; models: string[] }> = {}) =>
    ({ ...statusFixture().low_quota, ...over });

  it("says Auto's rule in one line", () => {
    expect(ruleLine(rule(), 2)).toBe("Cut when every watched account has less than 30 % left · every model");
    expect(ruleLine(rule({ rule: "any" }), 2)).toBe("Cut when any watched account has less than 30 % left · every model");
    expect(ruleLine(rule({ rule: "any" }), 1)).toBe("Cut when the watched account has less than 30 % left · every model");
    expect(ruleLine(rule(), null)).toBe("Cut when every watched account has less than 30 % left · every model");   // the count unread
    expect(ruleLine(rule({ models: ["a", "b", "c"] }), 2)).toBe("Cut when every watched account has less than 30 % left · 3 models");
    expect(ruleLine(rule({ models: ["a"] }), 2)).toBe("Cut when every watched account has less than 30 % left · 1 model");
    expect(ruleLine(rule(), 2, 45)).toBe("Cut when every watched account has less than 45 % left · every model");  // the slider's position
    const sentences = [
      ...MODES.map((m) => `${m.label} ${m.sentence}`), ruleLine(rule(), 2), ruleLine(rule({ models: ["a"] }), 1),
      NO_SECRET + NO_SECRET_AUTO, READ_FAILED_AUTO, UNWATCHED_MODES,
    ];
    for (const s of sentences) expect(s).not.toMatch(/\bfree\b|\bsav(e|ed|es|ing)\b/i);
  });

  it("places the slider where the core reports the mode", () => {
    const at45 = (routing: "direct" | "auto" | "relay") => statusFixture({ routing, low_quota: { ...statusFixture().low_quota, threshold_percent: 45 } });
    expect(position(at45("direct"))).toBe(0);
    expect(position(at45("relay"))).toBe(100);
    expect(position(at45("auto"))).toBe(45);
    expect(position(null)).toBeNull();
  });

  // Review Focus 14: the ends are modes and never thresholds.
  it("writes each end as a mode and a position between as Auto with its threshold", () => {
    expect(positionBody(0)).toEqual({ routing: "direct" });
    expect(positionBody(1)).toEqual({ routing: "auto", low_quota: { threshold_percent: 1 } });
    expect(positionBody(50)).toEqual({ routing: "auto", low_quota: { threshold_percent: 50 } });
    expect(positionBody(99)).toEqual({ routing: "auto", low_quota: { threshold_percent: 99 } });
    expect(positionBody(100)).toEqual({ routing: "relay" });
  });

  it("says each position's words and value text", () => {
    expect(positionWords(0, rule(), 2)).toBe(MODES[0].sentence);
    expect(positionWords(30, rule(), 2)).toBe("Cut when every watched account has less than 30 % left · every model");
    expect(positionWords(100, rule(), 2)).toBe(MODES[2].sentence);
    expect(positionText(30, rule(), 2)).toBe("Auto 30 %: Cut when every watched account has less than 30 % left · every model");
    expect(positionText(0, rule(), 2)).toBe(`Direct: ${MODES[0].sentence}`);
    expect(positionText(100, rule(), 2)).toBe(`Relay: ${MODES[2].sentence}`);
    expect([modeLabel("direct", 30), modeLabel("auto", 30), modeLabel("relay", 30)]).toEqual(["Direct", "Auto 30 %", "Relay"]);
  });
});
