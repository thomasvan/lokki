// Where CLIProxyAPI's own web panel is, for a browser that opens it by link.
import type { SettingsView } from "./api";

// Where the panel lives on CLIProxyAPI's origin on the pinned release.
export const PANEL_PATH = "/management.html";

// The page may be reached over a LAN or Tailscale address, so the port is joined to the
// page's own host: 127.0.0.1 would point at the viewer's machine.
// A configured URL that is not http or https gives no link (null): an href of
// `javascript:` would run in the UI's origin.
export function panelUrl(settings: SettingsView, loc: { hostname: string }): string | null {
  const url = typeof settings.cliproxy.url === "string" ? settings.cliproxy.url : "";
  if (url === "") return `http://${loc.hostname}:${String(settings.cliproxy.port)}${PANEL_PATH}`;
  try {
    if (!["http:", "https:"].includes(new URL(url).protocol)) return null;
  } catch {
    return null;
  }
  return url.replace(/\/+$/, "") + PANEL_PATH;
}
