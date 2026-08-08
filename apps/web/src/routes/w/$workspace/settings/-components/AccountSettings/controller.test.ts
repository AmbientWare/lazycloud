import { describe, expect, it } from "vitest";

import { passwordChangeBlocker } from "./controller";

describe("passwordChangeBlocker", () => {
  const filled = {
    currentPassword: "old-password",
    newPassword: "new-password",
    confirmPassword: "new-password",
  };

  it("permits a complete, consistent change", () => {
    expect(passwordChangeBlocker(filled)).toBe("");
  });

  it("refuses a new password that does not match its confirmation", () => {
    // Submitting anyway would set a password the person did not mean to type twice,
    // and they would find out by being locked out of their next sign-in.
    expect(passwordChangeBlocker({ ...filled, confirmPassword: "different" })).toContain(
      "do not match",
    );
  });

  it("refuses a new password identical to the current one", () => {
    expect(
      passwordChangeBlocker({
        currentPassword: "same-password",
        newPassword: "same-password",
        confirmPassword: "same-password",
      }),
    ).toContain("differ");
  });

  it("refuses one shorter than the server will accept", () => {
    expect(
      passwordChangeBlocker({ ...filled, newPassword: "short", confirmPassword: "short" }),
    ).toContain("at least 8");
  });

  it("asks for the current password first", () => {
    expect(passwordChangeBlocker({ ...filled, currentPassword: "" })).toContain(
      "current password",
    );
  });
});
