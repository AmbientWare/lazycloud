import { describe, expect, it } from "vitest";

import { regionOptions, regionsEqual, toggleAllowedRegion } from "./region-selection";

describe("AWS policy region selection", () => {

  it("adds and removes non-default regions without reordering the remaining values", () => {
    expect(toggleAllowedRegion(["us-east-1"], "us-west-2", "us-east-1")).toEqual([
      "us-east-1",
      "us-west-2",
    ]);
    expect(
      toggleAllowedRegion(
        ["us-east-1", "us-west-2", "eu-west-1"],
        "us-west-2",
        "us-east-1",
      ),
    ).toEqual(["us-east-1", "eu-west-1"]);
  });

  it("merges catalog and persisted choices once and compares ordered payloads", () => {
    expect(
      regionOptions(["us-east-1", "us-west-2"], ["us-east-1", "eu-west-1"]),
    ).toEqual(["us-east-1", "us-west-2", "eu-west-1"]);
    expect(regionsEqual(["us-east-1", "us-west-2"], ["us-east-1", "us-west-2"])).toBe(
      true,
    );
    expect(regionsEqual(["us-east-1", "us-west-2"], ["us-west-2", "us-east-1"])).toBe(
      false,
    );
  });
});
