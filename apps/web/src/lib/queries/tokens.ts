import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import {
  authTokenSchema,
  tokenCreateRequestSchema,
  tokenCreateResponseSchema,
  tokenListSchema,
  type AuthToken,
  type TokenCreateResponse,
} from "@/lib/api/schemas";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const COLLECTION = "/api/v1/tokens";

export type CreateTokenInput = {
  name: string;
  /** Lifetime in seconds, or null for a token that never expires. */
  expiresInSeconds: number | null;
};

function tokenPath(tokenId: string): string {
  return `${COLLECTION}/${encodeURIComponent(tokenId)}`;
}

/**
 * Every token the account holds, addressed without a workspace.
 *
 * A token reaches every workspace its account belongs to, so the list is the same
 * answer everywhere and stays out of the workspace keys that a switch invalidates.
 */
export function tokensQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.tokens(),
    queryFn: () => apiRequest(COLLECTION, tokenListSchema),
  });
}

export function createToken(input: CreateTokenInput): Promise<TokenCreateResponse> {
  const request = tokenCreateRequestSchema.parse({
    name: input.name.trim(),
    expires_in_seconds: input.expiresInSeconds,
  });
  return apiRequest(COLLECTION, tokenCreateResponseSchema, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function revokeToken(tokenId: string): Promise<AuthToken> {
  return apiRequest(`${tokenPath(tokenId)}/revoke`, authTokenSchema, { method: "POST" });
}

export function deleteToken(tokenId: string): Promise<null> {
  return apiRequest(tokenPath(tokenId), z.null(), { method: "DELETE" });
}
