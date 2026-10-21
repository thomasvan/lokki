import type { ActivityWindow } from "./api";

export const percent = (n: number | null) => (n === null ? "–" : `${Math.round(n)} %`);

// Built per call, so the viewer's zone is read when the time is shown.
export const clock = (epochS: number) =>
  new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(epochS * 1000));

const weekday = (epochS: number) => new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(new Date(epochS * 1000));

// A time seen from `nowS`: its clock within the last day, else its weekday and clock. It reads `nowS`,
// so a screen that draws it must move `nowS` with the clock.
export const stamp = (epochS: number, nowS: number) => (nowS - epochS < 86_400 ? clock(epochS) : `${weekday(epochS)} ${clock(epochS)}`);

// The label under bucket `k` of an activity answer, or null when it has none: 24 h marks every sixth
// hourly bucket with its clock time, 7 d every fourth six-hourly one with the weekday before it.
export function bucketLabel(start: number, window: ActivityWindow, k: number): string | null {
  if (window === "24h") return k % 6 === 0 ? clock(start) : null;
  if (k % 4 !== 0) return null;
  return `${weekday(start)} ${clock(start)}`;
}

export function age(seconds: number | null): string {
  if (seconds === null) return "never";
  if (seconds < 60) return `${Math.floor(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  return `${Math.floor(seconds / 3600)} h ago`;
}

// The time left to a reset: "1 h 12 min", "12 min", "under a minute".
export function resetsIn(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, "0")} min`;
}

// A token count: whole below a thousand, else "26 k" or "5.1 M" with one decimal under ten.
export function tokens(n: number): string {
  const unit = (value: number, suffix: string) => `${value < 10 ? Math.round(value * 10) / 10 : Math.round(value)} ${suffix}`;
  if (n < 1_000) return String(n);
  return n < 999_500 ? unit(n / 1_000, "k") : unit(n / 1_000_000, "M");
}

export const seconds = (s: number) => `${s.toFixed(1)} s`;

export const count = (n: number) => n.toLocaleString();
