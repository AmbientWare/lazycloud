import { describe, expect, it } from "vitest";

import { signedInDestination } from "./auth";

describe("signedInDestination", () => {
  it("returns to a path inside the dashboard as it was given", () => {
    expect(signedInDestination("/w/dev/apps?settings=tokens#logs")).toBe(
      "/w/dev/apps?settings=tokens#logs",
    );
    expect(signedInDestination("/w/dev/storage?path=a%2Fb")).toBe("/w/dev/storage?path=a%2Fb");
  });

  it.each([
    "https://evil.example/",
    "javascript:alert(1)",
    "//evil.example/w/dev",
    "/\\evil.example",
    "/\t/evil.example",
    "w/dev/apps",
    "",
  ])("sends %j to the dashboard instead of following it", (returnTo) => {
    expect(signedInDestination(returnTo)).toBe("/dashboard");
  });

  it("does not return a signed-in browser to a signed-out page", () => {
    for (const page of ["/", "/signin?error=access_denied", "/callback/"]) {
      expect(signedInDestination(page)).toBe("/dashboard");
    }
  });
});
