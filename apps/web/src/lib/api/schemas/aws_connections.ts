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

export const awsConnectionActionSchema = z.enum([
  "authorize",
  "validate",
  "reconnect",
  "cancel_reconnect",
  "remove",
  "retry",
]);
export type AwsConnectionAction = z.infer<typeof awsConnectionActionSchema>;

export const awsAuthorizationGenerationPhaseSchema = z.enum([
  "awaiting_authorization",
  "validating",
  "ready",
  "degraded",
  "retiring",
  "retired",
]);

export const awsAuthorizationErrorCodeSchema = z.enum([
  "assume_role_denied",
  "external_id_not_enforced",
  "account_mismatch",
  "permission_drift",
  "stack_drift",
  "upstream_unavailable",
]);

export const awsAuthorizationGenerationSchema = z
  .object({
    generation: z.number().int().positive(),
    authorization_mode: z.enum(["managed_stack", "existing_role"]),
    managed_authorization: z
      .object({
        stack_name: z.string().min(1).max(128),
        region: z.string().regex(/^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$/),
        generation: z.number().int().positive(),
        stack_id: z.string().min(1).max(2048).nullable(),
        template_version: z.string().min(1).max(128),
        template_sha256: z.string().regex(/^[0-9a-f]{64}$/),
      })
      .strict()
      .nullable(),
    phase: awsAuthorizationGenerationPhaseSchema,
    last_validation_started_at: z.string().nullable(),
    last_validated_at: z.string().nullable(),
    error_code: awsAuthorizationErrorCodeSchema.nullable(),
    error_message: z.string().max(512).nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type AwsAuthorizationGeneration = z.infer<typeof awsAuthorizationGenerationSchema>;

export const awsConnectionSchema = z
  .object({
    id: z.string().uuid(),
    account_id: z.string().regex(/^\d{12}$/),
    phase: awsConnectionPhaseSchema,
    active_authorization: awsAuthorizationGenerationSchema.nullable(),
    pending_authorization: awsAuthorizationGenerationSchema.nullable(),
    retiring_authorization: awsAuthorizationGenerationSchema.nullable(),
    revision: z.number().int().positive(),
    accepts_placement: z.boolean(),
    can_manage_existing_capacity: z.boolean(),
    available_actions: z.array(awsConnectionActionSchema),
    detail: z.string().max(512),
    customer_action: z
      .object({
        url: z.string().url().nullable(),
        label: z.string().min(1).max(128),
      })
      .strict()
      .nullable(),
    next_retry_at: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type AwsConnection = z.infer<typeof awsConnectionSchema>;

export const awsConnectionEnvelopeSchema = z
  .object({
    connection: awsConnectionSchema.nullable(),
  })
  .strict();
export type AwsConnectionEnvelope = z.infer<typeof awsConnectionEnvelopeSchema>;

export const awsConnectionAuthorizationSchema = z
  .object({
    connection: awsConnectionSchema,
    authorization: z
      .object({
        url: z.string().url().nullable(),
        external_id: z.string().min(32).max(256).nullable(),
      })
      .strict(),
  })
  .strict();
export type AwsConnectionAuthorization = z.infer<typeof awsConnectionAuthorizationSchema>;
