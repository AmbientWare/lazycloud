import { infiniteQueryOptions, type InfiniteData } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { nextPageCursor, selectPages } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const PAGE_SIZE = 50;

export type TokenPages = InfiniteData<Schemas["TokenList"], string>;

export type CreateTokenInput = {
  name: string;
  /** Lifetime in seconds, or null for a token that never expires. */
  expiresInSeconds: number | null;
};

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
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/tokens", {
          params: {
            query: {
              limit: PAGE_SIZE,
              include_device: includeDevice,
              cursor: pageParam || undefined,
            },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
  });
}

export function selectTokenList(data: TokenPages | undefined, hasNextPage: boolean | undefined) {
  return selectPages(
    data,
    (page) => page.tokens,
    hasNextPage,
    (token) => token.id,
  );
}

export function createToken(input: CreateTokenInput): Promise<Schemas["CreatedToken"]> {
  return ok(
    api.POST("/v1/tokens", {
      body: {
        name: input.name.trim(),
        expires_in_seconds: input.expiresInSeconds ?? undefined,
      },
    }),
  );
}

export function revokeToken(token: string): Promise<void> {
  return ok(api.DELETE("/v1/tokens/{token}", { params: { path: { token } } }));
}
