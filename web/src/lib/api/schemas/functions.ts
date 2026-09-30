import { z } from "zod";

import { jsonValueSchema } from "./json";
import { taskPendingProgressSchema } from "./task-progress";

export const functionResultEncodings = ["json", "cloudpickle"] as const;
export type FunctionResultEncoding = (typeof functionResultEncodings)[number];

const functionJsonResultSchema = z
  .object({
    version: z.literal(1).default(1),
    encoding: z.literal("json"),
    value: jsonValueSchema.default(null),
  })
  .strict();

const functionResultHtmlDisplaySchema = z
  .object({
    kind: z.literal("html"),
    html: z
      .string()
      .min(1)
      .max(256 * 1024),
  })
  .strict();

const functionResultImageDisplaySchema = z
  .object({
    kind: z.literal("image"),
    media_type: z.literal("image/png").default("image/png"),
    value_base64: z.string().min(1),
    size_bytes: z
      .number()
      .int()
      .min(1)
      .max(1024 * 1024),
  })
  .strict();

export const functionResultRichDisplaySchema = z.discriminatedUnion("kind", [
  functionResultHtmlDisplaySchema,
  functionResultImageDisplaySchema,
]);
export type FunctionResultRichDisplay = z.infer<typeof functionResultRichDisplaySchema>;

export const functionResultDisplaySchema = z
  .object({
    text: z.string().max(64 * 1024),
    rich: functionResultRichDisplaySchema.nullable().default(null),
  })
  .strict();
export type FunctionResultDisplay = z.infer<typeof functionResultDisplaySchema>;

const functionCloudpickleResultSchema = z
  .object({
    version: z.literal(1).default(1),
    encoding: z.literal("cloudpickle"),
    value_base64: z.string().default(""),
    size_bytes: z
      .number()
      .int()
      .min(0)
      .max(16 * 1024 * 1024),
    sha256: z.string().regex(/^[0-9a-f]{64}$/),
    display: functionResultDisplaySchema.nullable().default(null),
  })
  .strict();

export const functionResultSchema = z.discriminatedUnion("encoding", [
  functionJsonResultSchema,
  functionCloudpickleResultSchema,
]);
export type FunctionResult = z.infer<typeof functionResultSchema>;
export type FunctionCloudpickleResult = z.infer<typeof functionCloudpickleResultSchema>;

export const functionInvokeResponseSchema = z
  .object({
    task_id: z.string().default(""),
    output: z.string().default(""),
    stream: z.enum(["stdout", "stderr", "system"]).default("system"),
    status: z.string().default(""),
    pending_progress: taskPendingProgressSchema.nullable().default(null),
    done: z.boolean().default(false),
    exit_code: z.number().int().default(0),
    result: functionResultSchema.nullable().default(null),
  })
  .strict();
export type FunctionInvokeResponse = z.infer<typeof functionInvokeResponseSchema>;
