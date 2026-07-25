import { z } from "zod";

/** POST /api/v1/shells/existing-container — per-session shell credentials. */
export const shellSessionSchema = z.object({
  username: z.string(),
  password: z.string(),
  stub_id: z.string(),
  websocket_ticket: z.string().min(1),
}).strict();
export type ShellSession = z.infer<typeof shellSessionSchema>;
