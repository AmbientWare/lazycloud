import { describe, expect, it } from "vitest";

import {
  buildBody,
  clientContract,
  curlSnippet,
  playgroundFields,
  playgroundPythonOnlyReason,
  pythonOnlyReason,
  returnsPythonValue,
} from "./playground-form";

function contract(parameters: unknown[], operation: Record<string, unknown> = {}) {
  return clientContract({
    operation: { name: "remote", parameters, return_schema: {}, ...operation },
  });
}

it("keeps a Python result off HTTP but lets the playground invoke it", () => {
  const python = contract([], { return_schema: null, return_python_type: "numpy.ndarray" });
  expect(python).not.toBeNull();
  expect(pythonOnlyReason(python)).not.toBeNull();
  expect(playgroundPythonOnlyReason(python)).toBeNull();
  expect(returnsPythonValue(python)).toBe(true);
  expect(pythonOnlyReason(null)).toBeNull();
  expect(returnsPythonValue(null)).toBe(false);
});

it("reads no contract from a release that records an unreadable one", () => {
  expect(clientContract(undefined)).toBeNull();
  expect(clientContract({ operation: { parameters: "none" } })).toBeNull();
});

describe("playgroundFields", () => {
  it("maps flat primitive contract parameters to typed fields", () => {
    const fields = playgroundFields(
      contract([
        { name: "value", json_schema: { type: "integer" } },
        { name: "label", json_schema: { type: "string" }, required: false, default: "hi" },
      ]),
    );
    expect(fields).toEqual([
      { name: "value", type: "integer", required: true, defaultText: "" },
      { name: "label", type: "string", required: false, defaultText: '"hi"' },
    ]);
  });

  it("refuses a typed form when any parameter is not a flat primitive", () => {
    const fields = playgroundFields(
      contract([{ name: "values", json_schema: { type: "array", items: { type: "integer" } } }]),
    );
    expect(fields).toBeNull();
  });

  it("offers an empty form for no parameters and the JSON editor for no contract", () => {
    expect(playgroundFields(contract([]))).toEqual([]);
    expect(playgroundFields(null)).toBeNull();
  });
});

describe("buildBody", () => {
  const fields = [
    { name: "value", type: "integer" as const, required: true, defaultText: "" },
    { name: "ratio", type: "number" as const, required: false, defaultText: "" },
    { name: "label", type: "string" as const, required: false, defaultText: "" },
    { name: "flag", type: "boolean" as const, required: false, defaultText: "" },
  ];

  it("parses typed values and omits empty optional fields", () => {
    const built = buildBody(fields, { value: "4", ratio: "1.5", label: "", flag: "true" });
    expect(built.body).toEqual({ value: 4, ratio: 1.5, flag: true });
  });

  it("rejects missing required and malformed values", () => {
    expect(buildBody(fields, { value: "" }).error).toBe("value is required");
    expect(buildBody(fields, { value: "4.5" }).error).toBe("value must be an integer");
    expect(buildBody(fields, { value: "4", ratio: "abc" }).error).toBe("ratio must be a number");
    expect(buildBody(fields, { value: "4", flag: "yes" }).error).toBe("flag must be true or false");
  });
});

describe("snippets", () => {
  const url = "http://127.0.0.1:9000/v1/workspaces/dev/apps/demo/functions/square/invoke";

  it("shell-escapes single quotes in the payload", () => {
    const snippet = curlSnippet(url, { label: "it's" });
    expect(snippet).toContain(`-d '{"label":"it'\\''s"}'`);
  });
});
