import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const PAGE_SIZE = 50;

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
    getNextPageParam: (page) => page.next_cursor,
  });
}

export function createToken(input: CreateTokenInput): Promise<Schemas["CreatedToken"]> {
  return ok(
    api.POST("/v1/tokens", {
      body: {
        name: input.name.trim(),
        ...(input.expiresInSeconds === null ? {} : { expires_in_seconds: input.expiresInSeconds }),
      },
    }),
  );
}

export async function revokeToken(tokenId: string): Promise<null> {
  await ok(api.DELETE("/v1/tokens/{token}", { params: { path: { token: tokenId } } }));
  return null;
}
