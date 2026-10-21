import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { answers, hangs } from "../test-helpers";
import { ApiError, getAccounts, getEndpoint, putSettings, request, token } from "./api";

describe("the API module", () => {
  beforeEach(() => { sessionStorage.clear(); token.set(null); vi.restoreAllMocks(); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("sends the operator header only when it holds a token", async () => {
    const fetch = answers([200, { ok: true }], [200, { ok: true }]);
    await request("GET", "/api/status");
    expect(fetch.mock.calls[0][1].headers).not.toHaveProperty("X-Relay-Token");
    token.set("op-1");
    await request("GET", "/api/status");
    expect(fetch.mock.calls[1][1].headers).toMatchObject({ "X-Relay-Token": "op-1" });
    expect(fetch.mock.calls[1][0]).toBe("/api/status");            // same origin, no base URL
  });

  it("keeps the token in sessionStorage only, with an in-memory copy when storage throws", () => {
    token.set("op-2");
    expect(sessionStorage.getItem("lokki.token")).toBe("op-2");
    expect(localStorage.getItem("lokki.token")).toBeNull();
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(token.get()).toBe("op-2");
    vi.restoreAllMocks();
    token.set(null);
    expect(sessionStorage.getItem("lokki.token")).toBeNull();
    expect(token.get()).toBeNull();
  });

  it("turns the error envelope into an ApiError with its kind and key", async () => {
    answers(
      [401, { error: { message: "missing token", type: "invalid_request_error" } }],
      [403, { error: { message: "the Origin is not this relay's own", type: "invalid_request_error" } }],
      [409, { error: { message: "an existing cliproxy.url decides the upstream", type: "server_error" }, key: "cliproxy.port" }],
      new TypeError("Failed to fetch"));
    await expect(request("GET", "/api/status")).rejects.toMatchObject({ status: 401, kind: "auth" });
    await expect(request("GET", "/api/status")).rejects.toMatchObject({ status: 403, kind: "forbidden", message: "the Origin is not this relay's own" });
    const refused = await request("PUT", "/api/settings", { cliproxy: { port: 1 } }).catch((e) => e);
    expect(refused).toBeInstanceOf(ApiError);
    expect(refused).toMatchObject({ status: 409, kind: "refused", key: "cliproxy.port", message: "an existing cliproxy.url decides the upstream" });
    await expect(request("GET", "/api/status")).rejects.toMatchObject({ kind: "network" });
  });

  it("sends a PUT as JSON and a reveal as a query", async () => {
    const fetch = answers([200, {}], [200, {}], [200, {}]);
    await putSettings({ routing: "relay" });
    expect(fetch.mock.calls[0][0]).toBe("/api/settings");
    expect(fetch.mock.calls[0][1]).toMatchObject({ method: "PUT", body: JSON.stringify({ routing: "relay" }) });
    expect(fetch.mock.calls[0][1].headers).toMatchObject({ "Content-Type": "application/json" });
    await getEndpoint(true);
    expect(fetch.mock.calls[1][0]).toBe("/api/endpoint?reveal=1");
    await getAccounts();
    expect(fetch.mock.calls[2][0]).toBe("/api/accounts");
  });

  it("answers an empty 2xx body as null", async () => {
    answers([202, undefined]);
    expect(await request("POST", "/api/accounts/refresh")).toBeNull();
  });

  it("gives up on a core that does not answer in 20 s, as a network failure", async () => {
    vi.useFakeTimers();
    const fetch = hangs();
    const outcome = request("GET", "/api/status").catch((e) => e);
    await vi.advanceTimersByTimeAsync(19_999);
    expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await outcome).toMatchObject({ status: 0, kind: "network", message: "the core did not answer in time" });
  });

  it("refuses a 2xx answer that is not JSON, since a view cannot render it", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("<html>login</html>", { status: 200 }))));
    await expect(request("GET", "/api/status")).rejects.toMatchObject({ status: 200, kind: "other", message: "the answer was not JSON" });
  });
});
