import { z } from "zod";

export const awsConnectionPhaseSchema = z.enum([
  "awaiting_authorization",
  "validating",
  "ready",
  "degraded",
  "reconnect_pending",
  "retiring_authorization",
  "disconnect_draining",
  "revoking",
  "verifying_revocation",
  "action_required",
]);
