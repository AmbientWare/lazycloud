import { describe, expect, it } from "vitest";

import { decodeEncodedValue } from "./decode";

describe("decodeEncodedValue", () => {
  it("formats JSON without evaluating it", () => {
    const value = btoa('{"ready":true,"count":2}');
    expect(decodeEncodedValue(value)).toEqual({
      kind: "text",
      value: '{\n  "ready": true,\n  "count": 2\n}',
    });
  });

  it("describes binary payloads", () => {
    expect(decodeEncodedValue(btoa("\u0000\u0001\u0002"))).toEqual({
      kind: "binary",
      size: 3,
    });
  });
});
