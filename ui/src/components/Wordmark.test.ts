/// <reference types="node" />
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/svelte";
import Wordmark, { dotFor } from "./Wordmark.svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

afterEach(cleanup);

describe("Wordmark", () => {
  // The rule the mark follows: the gull from 64 px, the gull's 16 px drawing from 28 px, a plain dot below.
  it.each([
    [10, "dot"], [15, "dot"], [18, "dot"], [27.9, "dot"],
    [28, "gull16"], [32, "gull16"], [63.9, "gull16"],
    [64, "gull"], [96, "gull"],
  ])("draws the dot of the i for %s px as %s", (size, dot) => {
    expect(dotFor(size)).toBe(dot);
    render(Wordmark, { size });
    expect(document.querySelector("svg.wordmark")).toHaveAttribute("data-wordmark", dot);
  });

  it("is an image named Lokki, 0.8 em tall, its width following the drawing", () => {
    render(Wordmark, { size: 20 });
    const img = screen.getByRole("img", { name: "Lokki" });
    expect(img).toHaveAttribute("height", "16");
    expect(img).toHaveAttribute("width", String((20 * 0.8 * 196) / 80));
    render(Wordmark, { size: 64 });
    expect(screen.getAllByRole("img", { name: "Lokki" })[1]).toHaveAttribute("width", String((64 * 0.8 * 212) / 80));
  });

  it("draws the letters in currentColor and the dot in --accent", () => {
    render(Wordmark, { size: 15 });
    const [letters, dot] = Array.from(document.querySelectorAll("svg.wordmark path"));
    expect(letters).toHaveAttribute("stroke", "currentColor");
    expect(dot.getAttribute("style")).toContain("var(--accent)");
  });
});

const read = (path: string) => readFileSync(resolve(import.meta.dirname, path), "utf8");
const attrs = (tag: string) => Object.fromEntries([...tag.matchAll(/([\w-]+)="([^"]*)"/g)].map((m) => [m[1], m[2].replace(/\s+/g, " ").trim()]));
const paths = (text: string) => [...text.matchAll(/<path\b[^>]*>/g)].map((m) => attrs(m[0]));
const without = (tag: Record<string, string>, ...names: string[]) => Object.fromEntries(Object.entries(tag).filter(([k]) => !names.includes(k)));

// The component's drawings by name: the letters, then the dot of each size's rule.
const component = read("./Wordmark.svelte");
const drawing = (branch: string, until: string) => paths(component.slice(component.indexOf(branch), component.indexOf(until, component.indexOf(branch))));
const letters = paths(component.slice(0, component.indexOf("{#if dot")))[0];
const dots = {
  gull: drawing('{#if dot === "gull"}', "{:else if")[0],
  gull16: drawing('{:else if dot === "gull16"}', "{:else}")[0],
  dot: drawing("{:else}", "{/if}")[0],
};

it("tells the component's three dots apart", () => {
  expect(new Set(Object.values(dots).map((d) => d.d)).size).toBe(3);
  expect(letters.stroke).toBe("currentColor");
});

// error.html is not built from the UI, so it carries its own copy of the drawing it uses.
describe("the error page's copy of the wordmark", () => {
  const page = read("../../../app/src-tauri/dist/error.html");

  it("is the letters and the 16 px gull of Wordmark.svelte, attribute for attribute and no other dot", () => {
    expect(paths(page)).toEqual([letters, dots.gull16]);
  });

  it("is the same image: the 32 px size's box and its name", () => {
    const svg = attrs(page.match(/<svg\b[^>]*>/)![0]);
    expect(dotFor(32)).toBe("gull16");
    expect(svg).toMatchObject({ role: "img", "aria-label": "Lokki", viewBox: "0 -6 212 80", height: String(32 * 0.8), width: String((32 * 0.8 * 212) / 80) });
  });
});

// The README's drawings, one for each GitHub theme: the gull-dot wordmark in its own colours (sRGB, not currentColor or --accent).
describe.each(["light", "dark"])("the README's %s wordmark", (theme) => {
  const svg = read(`../../../docs/design/ux/wordmark-${theme}.svg`);

  it("is the letters and the 64 px gull of Wordmark.svelte, each but its colour, and no other dot", () => {
    const [ink, gull, ...more] = paths(svg);
    expect(more).toEqual([]);
    expect(without(ink, "stroke")).toEqual(without(letters, "stroke"));
    expect(ink.stroke).toMatch(/^#[0-9a-f]{6}$/);
    expect(without(gull, "fill")).toEqual(without(dots.gull, "style"));
    expect(gull.fill).toMatch(/^#[0-9a-f]{6}$/);
    expect(svg).toContain('viewBox="0 -6 212 80"');
    expect(svg).toContain('aria-label="Lokki"');
  });
});
