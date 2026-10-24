// The modes and the sentences the UI says about them (spec 3.1). Each view that says one of
// these imports it. No sentence says "free" or names a saving: a cut is cheaper than a
// finalized turn, never free (docs/relay/cost-of-a-cut.md).

export type Routing = "relay" | "auto" | "direct";

// In the slider's order, left to right.
export const MODES: ReadonlyArray<{ mode: Routing; label: string; sentence: string }> = [
  { mode: "direct", label: "Direct", sentence: "Every request goes to CLIProxyAPI untouched, at full price." },
  { mode: "auto", label: "Auto", sentence: "Cut when the watched accounts run low; otherwise pass through at full price." },
  { mode: "relay", label: "Relay", sentence: "Every request is cut: Lokki ends the answer early so it costs less." },
];

// A choice writes the mode alone, so Auto returns to the threshold the file holds.
export function modeBody(mode: Routing): Record<string, unknown> {
  return { routing: mode };
}

// What a choice is called: Auto carries the threshold it returns to, from the status read. No screen draws a
// choice before a status is read.
export function modeLabel(mode: Routing, threshold: number): string {
  const { label } = MODES.find((m) => m.mode === mode)!;
  return mode === "auto" ? `${label} ${threshold} %` : label;
}

// How many accounts Auto watches: the rows of the last accounts read that the core marks in_scope. The core
// is the one home of who is watched (a named account no row holds is not), so nothing here recomputes it.
// null while no read has answered. Structural types: the e2e imports this file, and its project takes no
// extensionless import.
export function watchedCount(accounts: { read_at: number | null; accounts: Array<{ in_scope: boolean }> } | null): number | null {
  if (!accounts || accounts.read_at === null) return null;
  return accounts.accounts.filter((a) => a.in_scope).length;
}

type Rule = { threshold_percent: number; rule: "all" | "any"; models: string[] };

// Auto's rule in one line (decision 12). `t` is the slider's position while it is held or stepped. It says
// "Cut", the word of Auto's sentence and of the decision's headline: Relay names a mode, which Auto is not.
export function ruleLine(lowQuota: Rule, watched: number | null, t = lowQuota.threshold_percent): string {
  const who = watched === 1 ? "the watched account" : lowQuota.rule === "any" ? "any watched account" : "every watched account";
  const n = lowQuota.models.length;
  const models = n === 0 ? "every model" : `${n} ${n === 1 ? "model" : "models"}`;
  return `Cut when ${who} has less than ${t} % left · ${models}`;
}

// The slider, 0 to 100 along "% left": Direct at 0, Relay at 100, Auto at its threshold; null with no status read.
export function position(status: { routing: Routing; low_quota: { threshold_percent: number } } | null): number | null {
  if (!status) return null;
  return status.routing === "direct" ? 0 : status.routing === "relay" ? 100 : status.low_quota.threshold_percent;
}

export const modeAt = (p: number): Routing => (p <= 0 ? "direct" : p >= 100 ? "relay" : "auto");

// What a position writes, in one PUT: the ends are modes and never thresholds (spec 3.1), so an end leaves the
// stored threshold for Auto to return to.
export function positionBody(p: number): Record<string, unknown> {
  const mode = modeAt(p);
  return mode === "auto" ? { routing: "auto", low_quota: { threshold_percent: p } } : modeBody(mode);
}

// The words under the slider: the mode's sentence at an end, the rule line between them.
export function positionWords(p: number, lowQuota: Rule, watched: number | null): string {
  const mode = modeAt(p);
  return mode === "auto" ? ruleLine(lowQuota, watched, p) : MODES.find((m) => m.mode === mode)!.sentence;
}

// The slider's aria-valuetext: its readout, then its words.
export const positionText = (p: number, lowQuota: Rule, watched: number | null) =>
  `${modeLabel(modeAt(p), p)}: ${positionWords(p, lowQuota, watched)}`;

export const headline = (relayOn: boolean) => (relayOn ? "Cutting every request" : "Passing requests through");

export const MODELS_NONE = "Nothing is picked yet, so every model is still cut when low";
export const MODELS_LEVEL = "A model at any thinking level, such as gpt-5.5(high), counts as gpt-5.5.";
export const NO_SECRET = "Lokki does not read the quota here: no management secret file is set";
export const NO_SECRET_AUTO = "; Auto passes every request through at full price";
export const READ_FAILED_AUTO = "; Auto counts an account it cannot read as not low, and passes requests through at full price for it";
export const UNWATCHED_MODES = "Auto watches accounts; choose Auto to pick them";
export const UNWRITABLE = "the settings file is not writable here";
