import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import type { ApiKey } from '@/interfaces/api-keys'

export const getApiKeys = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(async ({ context }): Promise<ApiKey[]> => {
    return lazycloudApi.getApiKeys(context.accessToken)
  })

export const regenerateApiKey = createServerFn({ method: 'POST' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      apiKeyId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<ApiKey> => {
    return lazycloudApi.regenerateApiKey(context.accessToken, data.apiKeyId)
  })
