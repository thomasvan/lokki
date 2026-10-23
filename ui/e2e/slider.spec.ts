import { test, expect } from "./core.ts";

// The mode slider's drag with a real mouse, in each engine the project runs. WKWebView, the macOS app's webview,
// is WebKit; ui.spec.ts holds the rest of the slider in Chromium.
test("a press, move and release drags the slider's thumb, and a later move with no button does not", async ({ page, core }) => {
  const puts: string[] = [];
  page.on("request", (r) => { if (r.method() === "PUT" && new URL(r.url()).pathname === "/api/settings") puts.push(r.postData() ?? ""); });
  await page.goto("/");
  await page.getByLabel("Operator token", { exact: true }).fill(core.token);
  await page.getByRole("button", { name: "Use", exact: true }).click();
  const slider = page.getByRole("slider", { name: "Mode and threshold" });
  await expect(slider).toHaveValue("30");

  const box = (await slider.boundingBox())!;
  const y = box.y + box.height / 2;
  const at = (p: number) => box.x + 10 + ((box.width - 20) * p) / 100;
  const value = async () => Number(await slider.inputValue());
  const near = (v: number, p: number) => Math.abs(v - p) <= 1;

  await page.mouse.move(at(30), y);
  await page.mouse.down();
  await page.mouse.move(at(45), y, { steps: 5 });
  expect(near(await value(), 45)).toBe(true);
  await page.mouse.move(at(60), y, { steps: 5 });
  expect(near(await value(), 60)).toBe(true);
  expect(puts).toEqual([]);
  await page.mouse.up();
  await expect.poll(() => puts.length).toBe(1);
  await expect(slider).toHaveAttribute("aria-busy", "false");
  const dropped = await value();
  expect(near(dropped, 60)).toBe(true);

  // Nothing is held now: the pointer moving over the slider, or away from it, leaves the thumb where it was dropped.
  await page.mouse.move(at(20), y, { steps: 5 });
  await page.mouse.move(at(80), y, { steps: 5 });
  await page.mouse.move(at(10), y + 60, { steps: 5 });
  expect(await value()).toBe(dropped);
  expect(puts).toHaveLength(1);

  // The core is the run's one, shared with the other projects: leave its threshold at 30.
  await slider.focus();
  for (let i = 0; i < dropped - 30; i++) await page.keyboard.press("ArrowLeft");
  await slider.blur();
  await expect.poll(() => puts.length).toBe(2);
  await expect(slider).toHaveAttribute("aria-busy", "false");
  await expect(slider).toHaveValue("30");
});
