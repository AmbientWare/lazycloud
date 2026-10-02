import { createEnv } from "@t3-oss/env-core";
import { z } from "zod";

export const viteEnv = createEnv({
  server: {
    /* The public API of the local platform, which `deploy/local/run.sh start`
       serves on port 8080. */
    VITE_API_TARGET: z.string().url().default("http://127.0.0.1:8080"),
    /* Comma-separated hostnames accepted for remote development. */
    VITE_DEV_ALLOWED_HOSTS: z.string().default(""),
  },
  runtimeEnv: process.env,
  emptyStringAsUndefined: true,
});
