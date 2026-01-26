import { createServerFn } from '@tanstack/react-start'
import { z } from 'zod'
import { authMiddleware } from '../middleware/auth'
import lazycloudApi from '../lazycloud-api'
import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
  WorkspaceCostBreakdownResponse,
  MeterPricingResponse,
} from '@/interfaces/usage'

export const getAggregatedUsage = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z
      .object({
        startDate: z.string().optional(),
        endDate: z.string().optional(),
      })
      .optional(),
  )
  .handler(async ({ context, data }): Promise<AggregatedUsageResponse> => {
    return lazycloudApi.getAggregatedUsage(
      context.accessToken,
      data?.startDate,
      data?.endDate,
    )
  })

export const getAggregatedDailyUsage = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z
      .object({
        startDate: z.string().optional(),
        endDate: z.string().optional(),
        timezone: z.string().optional(),
      })
      .optional(),
  )
  .handler(async ({ context, data }): Promise<AggregatedDailyUsageResponse> => {
    return lazycloudApi.getAggregatedDailyUsage(
      context.accessToken,
      data?.startDate,
      data?.endDate,
      data?.timezone,
    )
  })

export const getDeploymentCostBreakdown = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .inputValidator(
    z.object({
      deploymentId: z.string(),
      startDate: z.string().optional(),
      endDate: z.string().optional(),
    }),
  )
  .handler(
    async ({ context, data }): Promise<WorkspaceCostBreakdownResponse> => {
      return lazycloudApi.getDeploymentCostBreakdown(
        context.accessToken,
        data.deploymentId,
        data.startDate,
        data.endDate,
      )
    },
  )

// This function doesn't require auth
export const getMeterPricing = createServerFn({ method: 'GET' }).handler(
  async (): Promise<MeterPricingResponse> => {
    return lazycloudApi.getMeterPricing()
  },
)
