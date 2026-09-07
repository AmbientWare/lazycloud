import { z } from "zod";

const podFileInfoSchema = z.object({
  name: z.string(),
  size: z.number().default(0),
  is_dir: z.boolean().default(false),
  mod_time: z.string().nullish(),
  mode: z.number().default(0),
});
export type PodFileInfo = z.infer<typeof podFileInfoSchema>;

export const podFileListSchema = z.object({
  files: z.array(podFileInfoSchema).default([]),
});

export const podFileDownloadSchema = z.object({
  value_base64: z.string().default(""),
});
export type PodFileDownload = z.infer<typeof podFileDownloadSchema>;

const podProcessSchema = z.object({
  pid: z.number(),
  command: z.string().default(""),
});
export type PodProcess = z.infer<typeof podProcessSchema>;

export const podProcessListSchema = z.object({
  processes: z.array(podProcessSchema).default([]),
});

export const podUrlsSchema = z.object({
  urls: z.record(z.string()).default({}),
});
export type PodUrls = z.infer<typeof podUrlsSchema>;

export const podCreateImageSchema = z.object({ image_id: z.string() });
export const podMemorySnapshotSchema = z.object({ checkpoint_id: z.string() });
export const podEmptyMutationSchema = z.object({});

const sandboxRowSchema = z.object({
  id: z.string(),
  stub_id: z.string().nullish(),
  name: z.string(),
  created_at: z.string(),
  status: z.enum(["pending", "running", "stopping", "stopped", "failed"]),
  gpu: z.array(z.string()).default([]),
  container_id: z.string().nullish(),
  time_to_started_ms: z.number().nullish(),
  lifetime_ms: z.number().nullish(),
});
export type SandboxRow = z.infer<typeof sandboxRowSchema>;

export const sandboxListSchema = z.object({
  data: z.array(sandboxRowSchema).default([]),
  next: z.string().default(""),
});
