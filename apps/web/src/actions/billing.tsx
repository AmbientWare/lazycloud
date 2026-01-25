"use server";

import type { BillingCycleResponse } from "@/interfaces/billing";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";

export async function getBillingCycle(): Promise<BillingCycleResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getBillingCycle(accessToken);
}
