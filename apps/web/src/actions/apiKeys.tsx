"use server";

import lazycloudApi from "@/server/lazycloud_api";
import type { ApiKeyExpiresAtOptions } from "@/interfaces/apiKeys";
import { getUserId } from "./utils";

export async function getApiKeys() {
  const userId = await getUserId();
  return lazycloudApi.getApiKeys(userId);
}

export async function updateApiKey(
  tokenId: string,
  expiresAt: ApiKeyExpiresAtOptions,
) {
  const userId = await getUserId();
  return lazycloudApi.updateApiKey(userId, tokenId, expiresAt);
}

export async function createApiKey(
  name: string,
  expiresAt: ApiKeyExpiresAtOptions,
) {
  const userId = await getUserId();
  return lazycloudApi.createApiKey(userId, name, expiresAt);
}

export async function deleteApiKey(tokenId: string) {
  const userId = await getUserId();
  return lazycloudApi.deleteApiKey(userId, tokenId);
}
