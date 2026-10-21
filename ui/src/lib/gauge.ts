// What a gauge draws for one account. Once `reset_at` has passed, `policy.state_of` counts the
// account's remaining quota as 100 until the next read, while `remaining_percent` still holds the old
// reading; the gauge agrees with the chip the policy gives. An account the core does not read
// (`unknown`) keeps the number it had: the policy counts only a read one (spec 4.2).
import type { Account } from "./api";

export function gaugeOf(a: Account, nowS: number): { percent: number | null; windowReset: boolean } {
  const windowReset = a.state !== "unknown" && a.reset_at !== null && a.reset_at > 0 && a.reset_at <= nowS;
  return { percent: windowReset ? 100 : a.remaining_percent, windowReset };
}
