import { describe, expect, it } from "vitest";
import { settingsFixture } from "../test-helpers";
import { PANEL_PATH, panelUrl } from "./panel";

const withCliproxy = (cliproxy: Record<string, unknown>) => settingsFixture({ cliproxy });

describe("panelUrl", () => {
  it("links to the panel on the page's own host and the configured port", () => {
    expect(panelUrl(withCliproxy({ url: "", port: 8317 }), { hostname: "100.64.0.9" })).toBe(`http://100.64.0.9:8317${PANEL_PATH}`);
  });

  it("links to the configured URL when there is one, whatever the port says", () => {
    expect(panelUrl(withCliproxy({ url: "http://10.0.0.5:9000", port: 8317 }), { hostname: "100.64.0.9" })).toBe(`http://10.0.0.5:9000${PANEL_PATH}`);
    expect(panelUrl(withCliproxy({ url: "http://10.0.0.5:9000/", port: 8317 }), { hostname: "x" })).toBe(`http://10.0.0.5:9000${PANEL_PATH}`);
  });

  it("builds no link from a URL that is not http or https", () => {
    expect(panelUrl(withCliproxy({ url: "javascript:alert(1)", port: 8317 }), { hostname: "h" })).toBeNull();
    expect(panelUrl(withCliproxy({ url: "not a url", port: 8317 }), { hostname: "h" })).toBeNull();
  });
});
