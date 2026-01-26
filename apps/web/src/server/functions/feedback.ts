import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'

const feedbackTypes = ['bug', 'feature', 'other'] as const

export const submitFeedback = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      feedbackType: z.enum(feedbackTypes),
      message: z
        .string()
        .min(10, 'Message must be at least 10 characters')
        .max(5000, 'Message is too long (max 5000 characters)'),
    }),
  )
  .handler(async ({ context, data }) => {
    return lazycloudApi.submitFeedback(
      context.accessToken,
      data.feedbackType,
      data.message,
      'web',
    )
  })
