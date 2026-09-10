import { describe, expect, it } from "vitest";
import { z } from "zod";

import contractCasesJson from "../../../../../../tests/contracts/http_contract_cases.json";

import { errorResponseSchema } from "./errors";
import { functionInvokeResponseSchema } from "./functions";
import { jsonValueSchema } from "./json";
import { shellSessionSchema } from "./shells";

const contractNames = [
  "create_shell_in_existing_container_response",
  "error_response",
  "function_invoke_response",
] as const;

const contractCaseSchema = z.object({
  contract: z.enum(contractNames),
  name: z.string(),
  accepted: z.boolean(),
  input: z.record(jsonValueSchema),
  normalized: z.record(jsonValueSchema).nullable(),
});
const corpus = z
  .object({ format_version: z.literal(1), cases: z.array(contractCaseSchema) })
  .parse(contractCasesJson);
const schemaByContract = {
  create_shell_in_existing_container_response: shellSessionSchema,
  error_response: errorResponseSchema,
  function_invoke_response: functionInvokeResponseSchema,
} satisfies Record<(typeof contractNames)[number], z.ZodType>;

describe("Python-owned HTTP contract cases", () => {
  for (const testCase of corpus.cases) {
    it(`${testCase.contract}/${testCase.name}`, () => {
      const parsed = schemaByContract[testCase.contract].safeParse(testCase.input);
      expect(parsed.success).toBe(testCase.accepted);
      if (parsed.success) expect(parsed.data).toEqual(testCase.normalized);
      else expect(testCase.normalized).toBeNull();
    });
  }
});
