import { z } from "zod";

export const customDomainPhaseSchema = z.enum([
  "awaiting_verification",
  "validating",
  "ready",
  "action_required",
]);
export type CustomDomainPhase = z.infer<typeof customDomainPhaseSchema>;

export const customDomainErrorCodeSchema = z.enum([
  "verification_timed_out",
  "certificate_failed",
  "hostname_rejected",
  "upstream_unavailable",
]);
export type CustomDomainErrorCode = z.infer<typeof customDomainErrorCodeSchema>;

export const customDomainDnsModeSchema = z.enum(["cname", "delegation"]);
export type CustomDomainDnsMode = z.infer<typeof customDomainDnsModeSchema>;

export const dnsRecordSchema = z.object({
  type: z.string(),
  name: z.string(),
  value: z.string(),
});
export type DnsRecord = z.infer<typeof dnsRecordSchema>;

export const customDomainSchema = z.object({
  id: z.string(),
  hostname: z.string(),
  dns_mode: customDomainDnsModeSchema,
  phase: customDomainPhaseSchema,
  required_records: z.array(dnsRecordSchema).default([]),
  error_code: customDomainErrorCodeSchema.nullable().default(null),
  error_message: z.string().nullable().default(null),
  verified_at: z.string().datetime().nullable().default(null),
  last_checked_at: z.string().datetime().nullable().default(null),
  created_at: z.string().datetime(),
  updated_at: z.string().datetime(),
});
export type CustomDomain = z.infer<typeof customDomainSchema>;

export const customDomainListSchema = z.object({
  data: z.array(customDomainSchema).default([]),
  next: z.string().default(""),
});
export type CustomDomainList = z.infer<typeof customDomainListSchema>;
