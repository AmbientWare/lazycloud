import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import type { DeploymentStatusResponse } from '@/interfaces/deployments'

export const getDeploymentStatus = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      deploymentId: z.string(),
    }),
  )
  .handler(async ({ context, data }): Promise<DeploymentStatusResponse> => {
    return lazycloudApi.getDeploymentStatus(
      context.accessToken,
      data.deploymentId,
    )
  })
