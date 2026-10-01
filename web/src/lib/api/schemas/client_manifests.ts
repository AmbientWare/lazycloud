import { z } from "zod";

import { jsonValueSchema } from "./json";

// The callable contract the SDK records in a function's `client_contract`
// (python/shared/src/shared/http/client_manifests.py). The public API carries
// it as an open object, so the dashboard checks its shape here before the
// playground and call examples read it.

const clientParameterSchema = z
  .object({
    name: z.string(),
    json_schema: z.record(jsonValueSchema).nullable().default({}),
    python_type: z.string().default(""),
    required: z.boolean().default(true),
    default: jsonValueSchema.nullish(),
    python_default: z.boolean().default(false),
    parameter_kind: z.string().default("keyword"),
  })
  .refine(
    (parameter) => (parameter.json_schema === null) === Boolean(parameter.python_type),
    "A Python-only parameter must name its type and omit its JSON schema",
  )
  .refine(
    (parameter) => !parameter.python_default || (!parameter.required && parameter.default == null),
    "Python defaults remain in the handler",
  );
export type ClientParameter = z.infer<typeof clientParameterSchema>;

const clientOperationSchema = z
  .object({
    name: z.string(),
    parameters: z.array(clientParameterSchema).default([]),
    return_schema: z.record(jsonValueSchema).nullable().default({}),
    return_python_type: z.string().default(""),
  })
  .refine(
    (operation) => (operation.return_schema === null) === Boolean(operation.return_python_type),
    "A Python-only return must name its type and omit its JSON schema",
  );
export type ClientOperation = z.infer<typeof clientOperationSchema>;

export const clientContractSchema = z.object({
  operation: clientOperationSchema,
});
export type ClientContract = z.infer<typeof clientContractSchema>;
