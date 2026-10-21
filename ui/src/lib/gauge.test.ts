import { describe, expect, it } from "vitest";
import { accountFixture } from "../test-helpers";
import { gaugeOf } from "./gauge";

const NOW = 1_800_000_000;

describe("gaugeOf", () => {
  // The policy counts an account's remaining quota as 100 once 0 < reset_at <= now (policy.state_of),
  // and only for an account it reads: an unknown one keeps the number it last had.
  it.each([
    ["a passed reset", { state: "low", remaining_percent: 20, reset_at: NOW - 1 }, { percent: 100, windowReset: true }],
    ["a reset at this very second", { state: "low", remaining_percent: 20, reset_at: NOW }, { percent: 100, windowReset: true }],
    ["a reset still to come", { state: "low", remaining_percent: 20, reset_at: NOW + 60 }, { percent: 20, windowReset: false }],
    ["an unknown state", { state: "unknown", remaining_percent: 20, reset_at: NOW - 1 }, { percent: 20, windowReset: false }],
    ["no reading", { state: "unknown", remaining_percent: null, reset_at: null }, { percent: null, windowReset: false }],
    ["no reading, though the state is read", { state: "ok", remaining_percent: null, reset_at: NOW - 1 }, { percent: 100, windowReset: true }],
    ["no reset time", { state: "low", remaining_percent: 20, reset_at: null }, { percent: 20, windowReset: false }],
    ["a reset time of 0", { state: "low", remaining_percent: 20, reset_at: 0 }, { percent: 20, windowReset: false }],
  ] as const)("counts a passed reset as a full window, as the policy does (%s)", (_name, over, expected) => {
    expect(gaugeOf(accountFixture({ ...over }), NOW)).toEqual(expected);
  });
});
