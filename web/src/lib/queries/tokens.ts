import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type { AuthToken, TokenCreateResponse, TokenListResponse } from "@/lib/api/schemas";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const PAGE_SIZE = 50;

export type CreateTokenInput = {
  name: string;
  /** Lifetime in seconds, or null for a token that never expires. */
  expiresInSeconds: number | null;
};

/**
 * A token as the dashboard lists it. The API keeps only a digest, so a listed
 * token has no prefix to show; a freshly issued one is recognized by its own
 * first characters.
 */
function viewToken(token: Schemas["Token"], prefix = ""): AuthToken {
  return {
    id: token.id,
    name: token.name,
    prefix,
    kind: token.workspace_id ? "workspace" : "user",
    device_login: token.device,
    user_id: "",
    workspace_id: token.workspace_id ?? "",
    status: token.status,
    scopes: [],
    reusable: true,
    disabled_by_admin: false,
    created_at: token.created_at,
    last_used_at: token.last_used_at ?? null,
    expires_at: token.expires_at ?? null,
    revoked_at: null,
  } as AuthToken;
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
    queryFn: async ({ pageParam }): Promise<TokenListResponse> => {
      const page = await ok(
        api.GET("/v1/tokens", {
          params: {
            query: {
              limit: PAGE_SIZE,
              include_device: includeDevice,
              cursor: pageParam || undefined,
            },
          },
        }),
      );
      return { data: page.tokens.map((token) => viewToken(token)), next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
  });
}

export function selectTokenList(
  data: InfiniteListQueryData<AuthToken> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (token) => token.id);
}

export async function createToken(input: CreateTokenInput): Promise<TokenCreateResponse> {
  const created = await ok(
    api.POST("/v1/tokens", {
      body: {
        name: input.name.trim(),
        ...(input.expiresInSeconds === null ? {} : { expires_in_seconds: input.expiresInSeconds }),
      },
    }),
  );
  return { token: created.token, record: viewToken(created.record, created.token.slice(0, 6)) };
}

export async function revokeToken(tokenId: string): Promise<AuthToken> {
  await ok(api.DELETE("/v1/tokens/{token}", { params: { path: { token: tokenId } } }));
  return viewToken({
    id: tokenId,
    name: "",
    device: false,
    status: "expired",
    created_at: new Date().toISOString(),
  });
}
