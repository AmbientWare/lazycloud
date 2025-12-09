"use server";

import type {
  AggregatedUsageResponse,
  AggregatedDailyUsageResponse,
  WorkspaceCostBreakdownResponse,
  MeterPricingResponse,
} from "@/interfaces/usage";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";

export async function getAggregatedUsage(
  startDate?: string,
  endDate?: string,
): Promise<AggregatedUsageResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getAggregatedUsage(accessToken, startDate, endDate);
}

export async function getAggregatedDailyUsage(
  startDate?: string,
  endDate?: string,
  timezone?: string,
): Promise<AggregatedDailyUsageResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getAggregatedDailyUsage(accessToken, startDate, endDate, timezone);
}

export async function getDeploymentCostBreakdown(
  deploymentId: string,
  startDate?: string,
  endDate?: string,
): Promise<WorkspaceCostBreakdownResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getDeploymentCostBreakdown(
    accessToken,
    deploymentId,
    startDate,
    endDate,
  );
}

export async function getMeterPricing(): Promise<MeterPricingResponse> {
  return lazycloudApi.getMeterPricing();
}
