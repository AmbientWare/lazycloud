"use server";

import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
  WorkspaceCostBreakdownResponse,
  MeterPricingResponse,
} from "@/interfaces/usage";
import lazycloudApi from "@/server/lazycloud_api";
import { getUserId } from "./utils";

export async function getAggregatedUsage(
  startDate?: string,
  endDate?: string,
): Promise<AggregatedUsageResponse> {
  const userId = await getUserId();
  return lazycloudApi.getAggregatedUsage(userId, startDate, endDate);
}

export async function getAggregatedDailyUsage(
  startDate?: string,
  endDate?: string,
  timezone?: string,
): Promise<AggregatedDailyUsageResponse> {
  const userId = await getUserId();
  return lazycloudApi.getAggregatedDailyUsage(userId, startDate, endDate, timezone);
}

export async function getDeploymentCostBreakdown(
  deploymentId: string,
  startDate?: string,
  endDate?: string,
): Promise<WorkspaceCostBreakdownResponse> {
  const userId = await getUserId();
  return lazycloudApi.getDeploymentCostBreakdown(
    userId,
    deploymentId,
    startDate,
    endDate,
  );
}

export async function getMeterPricing(): Promise<MeterPricingResponse> {
  return lazycloudApi.getMeterPricing();
}
