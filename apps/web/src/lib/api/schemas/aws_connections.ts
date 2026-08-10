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

const awsInstanceTypeSchema = z
  .string()
  .min(1)
  .refine((value) => value.trim().length > 0, "Instance type cannot be empty");
const awsDefaultInstanceTypeSchema = z
  .string()
  .min(1)
  .max(64)
  .refine((value) => value.trim().length > 0, "Instance type cannot be empty");

/** How capacity is provisioned in the connected account, for every workspace it backs. */
export const awsComputeConfigurationSchema = z
  .object({
    revision: z.number().int().positive(),
    default_region: z.string(),
    default_instance_type: awsDefaultInstanceTypeSchema,
    initial_cpu_workers: z.number().int().min(0).max(100),
    min_cpu_workers: z.number().int().min(0).max(100),
    max_cpu_instances: z.number().int().min(0).max(100),
    max_gpu_instances: z.number().int().min(0).max(100),
    min_free_cpu_millicores: z.number().int().min(0),
    min_free_memory_mib: z.number().int().min(0),
    allowed_regions: z.array(z.string()).min(1),
    allowed_instance_types: z
      .array(awsInstanceTypeSchema)
      .refine(
        (instanceTypes) => new Set(instanceTypes).size === instanceTypes.length,
        "Allowed instance types must be unique",
      ),
    idle_timeout_seconds: z.number().int().min(60).max(86_400),
    root_volume_gib: z.number().int().min(50).max(2048),
  })
  .strict()
  .superRefine((configuration, context) => {
    if (
      configuration.min_cpu_workers > configuration.initial_cpu_workers ||
      configuration.initial_cpu_workers > configuration.max_cpu_instances
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "CPU worker capacity must satisfy min <= initial <= max",
        path: ["initial_cpu_workers"],
      });
    }
    if (
      configuration.allowed_instance_types.length > 0 &&
      !configuration.allowed_instance_types.includes(configuration.default_instance_type)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Default instance type must be allowed",
        path: ["default_instance_type"],
      });
    }
  });
export type AwsComputeConfiguration = z.infer<typeof awsComputeConfigurationSchema>;

export const awsComputeConfigurationUpdateRequestSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    compute: awsComputeConfigurationSchema,
  })
  .strict();
export type AwsComputeConfigurationUpdateRequest = z.infer<
  typeof awsComputeConfigurationUpdateRequestSchema
>;

export const awsConnectionSchema = z
  .object({
    id: z.string().uuid(),
    account_id: z.string().regex(/^\d{12}$/),
    phase: awsConnectionPhaseSchema,
    active_authorization: awsAuthorizationGenerationSchema.nullable(),
    pending_authorization: awsAuthorizationGenerationSchema.nullable(),
    retiring_authorization: awsAuthorizationGenerationSchema.nullable(),
    revision: z.number().int().positive(),
    compute: awsComputeConfigurationSchema,
    hosts_workloads: z.boolean(),
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
