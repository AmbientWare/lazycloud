import { z } from "zod";

export const ApiKeySchema = z.object({
  id: z.string(),
  name: z.string(),
  value: z.string(),
  expires_at: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export type ApiKey = z.infer<typeof ApiKeySchema>;
