import { z } from "zod";

import { jsonValueSchema } from "./json";

// Synced to packages/shared/src/shared/http/client_manifests.py
// Includes the callable contract used by the playground and call examples.

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

const clientContractSchema = z.object({
  operation: clientOperationSchema,
});
export type ClientContract = z.infer<typeof clientContractSchema>;

/** One field of the SDK `Schema` metadata dict (`{"fields": {name: {type}}}`). */
const manifestSchemaFieldSchema = z.object({
  type: z.string().default(""),
});
export type ManifestSchemaField = z.infer<typeof manifestSchemaFieldSchema>;

const manifestSchemaSchema = z.object({
  fields: z.record(manifestSchemaFieldSchema).default({}),
});
export type ManifestSchema = z.infer<typeof manifestSchemaSchema>;

export const deploymentManifestSchema = z.object({
  app: z.string(),
  name: z.string(),
  kind: z.string(),
  stub_id: z.string(),
  deployment_id: z.string(),
  deployment_version: z.number(),
  invoke_url: z.string(),
  invoke_path: z.string(),
  timeout_seconds: z.number().int().nonnegative().nullable().default(null),
  methods: z.array(z.string()).default([]),
  inputs: manifestSchemaSchema.default({ fields: {} }),
  client_contract: clientContractSchema.nullish(),
});
export type DeploymentManifest = z.infer<typeof deploymentManifestSchema>;
