import { z } from "zod";

export const deploymentKindSchema = z.enum([
  "function",
  "endpoint",
  "asgi",
  "pod",
  "sandbox",
  "command",
]);
export type DeploymentKind = z.infer<typeof deploymentKindSchema>;
