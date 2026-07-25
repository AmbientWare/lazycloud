import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, withWorkspace } from "@/lib/api/client";
import {
  authTokenSchema,
  tokenCreateResponseSchema,
  tokenListSchema,
  workspaceTokenCreateRequestSchema,
  type AuthToken,
  type TokenCreateResponse,
} from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

export type CreateTokenInput = {
  name: string;
  scopes: Array<"read" | "write">;
  expiresInSeconds: number | null;
};

export function tokensQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.settings.tokens(workspaceId),
    queryFn: () => apiRequest(withWorkspace("/api/v1/tokens", workspaceId), tokenListSchema),
  });
}

export function createToken(
  workspaceId: string,
  input: CreateTokenInput,
): Promise<TokenCreateResponse> {
  const request = workspaceTokenCreateRequestSchema.parse({
    name: input.name,
    scopes: input.scopes,
    expires_in_seconds: input.expiresInSeconds,
    kind: "workspace",
    workspace_id: workspaceId,
    reusable: true,
  });
  return apiRequest(withWorkspace("/api/v1/tokens", workspaceId), tokenCreateResponseSchema, {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export function toggleToken(workspaceId: string, tokenId: string): Promise<AuthToken> {
  return apiRequest(
    withWorkspace(`/api/v1/tokens/${encodeURIComponent(tokenId)}/toggle`, workspaceId),
    authTokenSchema,
    { method: "POST" },
  );
}

export function deleteToken(workspaceId: string, tokenId: string): Promise<null> {
  return apiRequest(
    withWorkspace(`/api/v1/tokens/${encodeURIComponent(tokenId)}`, workspaceId),
    z.null(),
    { method: "DELETE" },
  );
}
