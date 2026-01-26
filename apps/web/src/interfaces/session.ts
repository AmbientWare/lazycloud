import { z } from 'zod'

export const sessionMetadataSchema = z.object({
  onboardingComplete: z.boolean().optional(),
  stripeCustomerId: z.string().optional(),
})

export const sessionDataSchema = z.object({
  userId: z.string(),
  metadata: sessionMetadataSchema,
})

export type SessionMetadata = z.infer<typeof sessionMetadataSchema>
export type SessionData = z.infer<typeof sessionDataSchema>
