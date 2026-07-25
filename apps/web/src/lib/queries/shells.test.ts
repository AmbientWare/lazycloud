import { describe, expect, it } from "vitest";

import { shellWebSocketUrl } from "@/lib/queries/shells";

describe("shell WebSocket credentials", () => {
  it("puts only the one-use ticket in the browser WebSocket URL", () => {
    localStorage.setItem("lazycloud.auth.token", "long-lived-secret");

    const url = shellWebSocketUrl("stub/1", "container/1", "wst_one-use");

    expect(url).toContain("?ticket=wst_one-use");
    expect(url).not.toContain("long-lived-secret");
    expect(url).not.toContain("token=");
    expect(url).not.toContain("authorization=");
  });
});
