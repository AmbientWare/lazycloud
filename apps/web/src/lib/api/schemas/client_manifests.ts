import { z } from "zod";

import { jsonValueSchema } from "./json";

// Synced to packages/shared/src/shared/http/client_manifests.py
// (ClientManifestResource) and shared/models/client_contracts.py; scoped to
// the fields the function-page playground consumes.

const clientParameterSchema = z.object({
  name: z.string(),
  json_schema: z.record(jsonValueSchema).default({}),
  required: z.boolean().default(true),
  default: jsonValueSchema.nullish(),
  parameter_kind: z.string().default("keyword"),
});
export type ClientParameter = z.infer<typeof clientParameterSchema>;

const clientOperationSchema = z.object({
  name: z.string(),
  parameters: z.array(clientParameterSchema).default([]),
  return_schema: z.record(jsonValueSchema).default({}),
});
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
  methods: z.array(z.string()).default([]),
  inputs: manifestSchemaSchema.default({ fields: {} }),
  client_contract: clientContractSchema.nullish(),
});
export type DeploymentManifest = z.infer<typeof deploymentManifestSchema>;
