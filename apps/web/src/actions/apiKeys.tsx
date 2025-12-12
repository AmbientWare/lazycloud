"use server";

import type { ApiKey } from "@/interfaces/apiKeys";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";

export async function getApiKeys(): Promise<ApiKey[]> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getApiKeys(accessToken);
}

export async function regenerateApiKey(apiKeyId: string): Promise<ApiKey> {
  const accessToken = await getAuthToken();
  return lazycloudApi.regenerateApiKey(accessToken, apiKeyId);
}
