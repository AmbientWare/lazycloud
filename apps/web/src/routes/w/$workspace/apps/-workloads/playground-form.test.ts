import { describe, expect, it } from "vitest";

import type { DeploymentManifest } from "@/lib/api/schemas";

import { buildBody, curlSnippet, playgroundFields } from "./playground-form";

function manifest(overrides: Partial<DeploymentManifest> = {}): DeploymentManifest {
  return {
    app: "demo",
    name: "square",
    kind: "function",
    stub_id: "stub-1",
    deployment_id: "deployment-1",
    deployment_version: 1,
    invoke_url: "https://square-a1b2c3d4.lazycloud.dev",
    invoke_path: "/api/v1/functions/square/latest",
    methods: [],
    inputs: { fields: {} },
    client_contract: null,
    ...overrides,
  };
}

function contractManifest(
  parameters: NonNullable<DeploymentManifest["client_contract"]>["operation"]["parameters"],
): DeploymentManifest {
  return manifest({
    client_contract: {
      operation: { name: "remote", parameters, return_schema: {} },
    },
  });
}

describe("playgroundFields", () => {
  it("maps flat primitive contract parameters to typed fields", () => {
    const fields = playgroundFields(
      contractManifest([
        {
          name: "value",
          json_schema: { type: "integer" },
          required: true,
          default: null,
          parameter_kind: "keyword",
        },
        {
          name: "label",
          json_schema: { type: "string" },
          required: false,
          default: "hi",
          parameter_kind: "keyword",
        },
      ]),
    );
    expect(fields).toEqual([
      { name: "value", type: "integer", required: true, defaultText: "" },
      { name: "label", type: "string", required: false, defaultText: '"hi"' },
    ]);
  });

  it("refuses a typed form when any parameter is not a flat primitive", () => {
    const fields = playgroundFields(
      contractManifest([
        {
          name: "values",
          json_schema: { type: "array", items: { type: "integer" } },
          required: true,
          default: null,
          parameter_kind: "keyword",
        },
      ]),
    );
    expect(fields).toBeNull();
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
  const url = "http://127.0.0.1:9000/api/v1/functions/public/stub-1";

  it("shell-escapes single quotes in the payload", () => {
    const snippet = curlSnippet(url, { label: "it's" });
    expect(snippet).toContain(`-d '{"label":"it'\\''s"}'`);
  });
});
