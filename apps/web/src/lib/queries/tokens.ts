import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  authTokenSchema,
  tokenCreateRequestSchema,
  tokenCreateResponseSchema,
  tokenListSchema,
  type AuthToken,
  type TokenCreateResponse,
  type TokenListResponse,
} from "@/lib/api/schemas";
import { selectInfiniteList, type InfiniteListQueryData } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const COLLECTION = "/api/v1/tokens";
const PAGE_SIZE = 50;

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
export function tokensQueryOptions(includeDevice: boolean) {
  return infiniteQueryOptions({
    queryKey: [...accountQueryKeys.tokens(), { includeDevice }],
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        include_device: String(includeDevice),
      });
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(`${COLLECTION}?${params.toString()}`, tokenListSchema);
    },
    getNextPageParam: nextTokenCursor,
  });
}

/** A repeated cursor would page forever, so a server that returns one ends the list. */
function nextTokenCursor(
  lastPage: TokenListResponse,
  pages: TokenListResponse[],
): string | undefined {
  if (!lastPage.next) return undefined;
  const cursorAlreadySeen = pages.slice(0, -1).some((page) => page.next === lastPage.next);
  return cursorAlreadySeen ? undefined : lastPage.next;
}

export function selectTokenList(
  data: InfiniteListQueryData<AuthToken> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (token) => token.id);
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
