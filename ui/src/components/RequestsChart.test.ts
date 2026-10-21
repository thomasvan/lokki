import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/svelte";
import { activityFixture } from "../test-helpers";
import RequestsChart from "./RequestsChart.svelte";

describe("RequestsChart", () => {
  afterEach(cleanup);

  // The bars and their labels come from the answer's own window, never from the screen that draws it.
  it("draws a 7 d answer as 28 six-hour bars labelled every fourth with a weekday", () => {
    const { container } = render(RequestsChart, { activity: activityFixture({}, { 5: { relay: 2 } }, "7d") });
    expect(container.querySelectorAll("[data-bucket]")).toHaveLength(28);
    const labels = [...container.querySelectorAll(".chart-x i")].map((i) => i.textContent);
    expect(labels).toHaveLength(7);
    for (const label of labels) expect(label).toMatch(/^\p{L}+ \d\d:\d\d$/u);
    expect(screen.getByRole("img")).toHaveAccessibleName("Requests per 6 hours over the last 7 days, stacked by route");
  });

  it("draws a 24 h answer as 24 hourly bars labelled every sixth with a clock time", () => {
    const { container } = render(RequestsChart, { activity: activityFixture() });
    expect(container.querySelectorAll("[data-bucket]")).toHaveLength(24);
    expect([...container.querySelectorAll(".chart-x i")].map((i) => i.textContent)).toHaveLength(4);
    expect(screen.getByRole("img")).toHaveAccessibleName("Requests per hour over the last 24 hours, stacked by route");
  });
});
