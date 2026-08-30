import { createEnv } from "@t3-oss/env-core";
import { z } from "zod";

export const viteEnv = createEnv({
  server: {
    /* Root Compose publishes the control plane's container port 9000 on host
       port 8000. Keep custom targets explicit, but make the normal dev server
       reach the production-shaped local stack without extra configuration. */
    VITE_API_TARGET: z.string().url().default("http://127.0.0.1:8000"),
    /* Vite rejects a request whose Host header it was not told to expect, so
       reaching the dev server by machine name over a private network fails
       until that name is listed. Comma separated. */
    VITE_DEV_ALLOWED_HOSTS: z.string().default(""),
  },
  runtimeEnv: process.env,
  emptyStringAsUndefined: true,
});
