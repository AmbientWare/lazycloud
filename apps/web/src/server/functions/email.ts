import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import resendService from '../resend-service'
import { supportRatelimit } from '@/lib/rate-limit'

async function checkEmailRateLimit(
  email: string,
): Promise<{ allowed: boolean; message?: string }> {
  if (import.meta.env.MODE === 'development' || !supportRatelimit) {
    return { allowed: true }
  }

  const identifier = `email:${email.toLowerCase()}`
  const { success } = await supportRatelimit.limit(identifier)

  if (!success) {
    return {
      allowed: false,
      message:
        'Too many email requests. Please wait before submitting another request.',
    }
  }

  return { allowed: true }
}

export const sendSupportEmail = createServerFn({ method: 'POST' })
  .inputValidator(
    z.object({
      email: z.string().email(),
      description: z
        .string()
        .min(10)
        .max(5000)
        .refine(
          (val) => {
            const suspiciousPatterns = [
              /(http|https):\/\//gi,
              /\[url\]/gi,
              /<script/gi,
              /javascript:/gi,
            ]
            return !suspiciousPatterns.some((pattern) => pattern.test(val))
          },
          { message: 'Description contains invalid content' },
        ),
    }),
  )
  .handler(async ({ data }) => {
    const rateLimit = await checkEmailRateLimit(data.email)
    if (!rateLimit.allowed) {
      throw new Error(rateLimit.message ?? 'Rate limit exceeded')
    }

    const subject = 'SUPPORT REQUESTED'
    const title = 'The following user has requested support:'
    const body = `Email: ${data.email}\nDescription: ${data.description}`

    return resendService.emailSupport(subject, title, body)
  })

export const sendEnterpriseInquiry = createServerFn({ method: 'POST' })
  .inputValidator(
    z.object({
      email: z.string().email(),
      name: z.string().optional(),
      workosId: z.string().optional(),
      message: z.string().optional(),
    }),
  )
  .handler(async ({ data }) => {
    const rateLimit = await checkEmailRateLimit(data.email)
    if (!rateLimit.allowed) {
      throw new Error(rateLimit.message ?? 'Rate limit exceeded')
    }

    const subject = 'ENTERPRISE INQUIRY'
    const title =
      'The following user has requested Enterprise plan information:'
    let body = `Email: ${data.email}`
    if (data.name) {
      body += `\nName: ${data.name}`
    }
    if (data.workosId) {
      body += `\nWorkOS ID: ${data.workosId}`
    }
    if (data.message?.trim()) {
      body += `\n\nMessage:\n${data.message.trim()}`
    }

    return resendService.emailSupport(subject, title, body)
  })
