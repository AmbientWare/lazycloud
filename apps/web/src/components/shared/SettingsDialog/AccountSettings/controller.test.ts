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

  // Kept because no server owner proves it: the API accepts a password equal to the
  // current one, so this is the only place the person is told.
  it("refuses a new password identical to the current one", () => {
    expect(
      passwordChangeBlocker({
        currentPassword: "same-password",
        newPassword: "same-password",
        confirmPassword: "same-password",
      }),
    ).toContain("differ");
  });
});
