import { describe, expect, it, vi } from "vitest";
import { age, bucketLabel, clock, percent, resetsIn, stamp, tokens } from "./format";

describe("format", () => {
  it("shows a percent, rounded, and a dash for a missing number", () => {
    expect(percent(80)).toBe("80 %");
    expect(percent(33.4)).toBe("33 %");
    expect(percent(null)).toBe("–");
  });

  it("shows an age in the largest whole unit, and never for null", () => {
    expect(age(12)).toBe("12 s ago");
    expect(age(400)).toBe("6 min ago");
    expect(age(7300)).toBe("2 h ago");
    expect(age(null)).toBe("never");
  });

  it("shows the time to a reset in hours and minutes, minutes alone, or under a minute", () => {
    expect(resetsIn(4320)).toBe("1 h 12 min");
    expect(resetsIn(5400)).toBe("1 h 30 min");
    expect(resetsIn(7500)).toBe("2 h 05 min");
    expect(resetsIn(7200)).toBe("2 h 00 min");
    expect(resetsIn(3599)).toBe("59 min");
    expect(resetsIn(720)).toBe("12 min");
    expect(resetsIn(60)).toBe("1 min");
    expect(resetsIn(59)).toBe("under a minute");
    expect(resetsIn(0)).toBe("under a minute");
  });

  it("shows a time as its clock within the last day and with its weekday before that", () => {
    const at = 1_800_000_000;
    const day = new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(new Date(at * 1000));
    expect(stamp(at, at + 3600)).toBe(clock(at));
    expect(stamp(at, at + 86_399)).toBe(clock(at));
    expect(stamp(at, at + 86_400)).toBe(`${day} ${clock(at)}`);
    expect(stamp(at, at - 5)).toBe(clock(at));                    // a clock a little behind the core's is not "a day old"
  });

  it("shows a token count whole, in thousands or in millions", () => {
    expect([0, 999, 1_000, 1_204, 26_000, 380_000, 999_400, 999_500, 5_100_000, 41_000_000].map(tokens))
      .toEqual(["0", "999", "1 k", "1.2 k", "26 k", "380 k", "999 k", "1 M", "5.1 M", "41 M"]);
  });

  it("shows a clock time as 24-hour HH:MM in the viewer's zone, half-hour offsets included", () => {
    try {
      vi.stubEnv("TZ", "UTC");
      expect(clock(0)).toBe("00:00");
      expect(clock(13 * 3600 + 5 * 60 + 59)).toBe("13:05");
      expect(clock(23 * 3600 + 59 * 60)).toBe("23:59");
      vi.stubEnv("TZ", "Asia/Kolkata");
      expect(clock(0)).toBe("05:30");
    } finally {
      vi.unstubAllEnvs();
    }
  });

  // A bucket's label is its start in the viewer's zone: 24 h marks every sixth hourly bucket, 7 d every
  // fourth six-hourly one with the weekday (spec 4.3). `start` is 2027-01-15 08:00 UTC.
  it("labels every sixth bucket of 24 h with a clock time and every fourth of 7 d with the weekday too", () => {
    const start = 1_800_000_000;
    try {
      vi.stubEnv("TZ", "UTC");
      expect([0, 6, 12, 18].map((k) => bucketLabel(start + k * 3600, "24h", k))).toEqual(["08:00", "14:00", "20:00", "02:00"]);
      expect([1, 5, 7, 23].map((k) => bucketLabel(start + k * 3600, "24h", k))).toEqual([null, null, null, null]);
      expect(bucketLabel(start, "7d", 0)).toMatch(/^\p{L}+ 08:00$/u);
      expect(bucketLabel(start + 4 * 6 * 3600, "7d", 4)).toMatch(/^\p{L}+ 08:00$/u);
      expect([1, 2, 3, 6].map((k) => bucketLabel(start + k * 6 * 3600, "7d", k))).toEqual([null, null, null, null]);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});
