import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { accountQueryKeys, EVERY_ACCOUNT, type AccountScope } from "@/lib/queries/workspace-keys";

const PAGE_SIZE = 50;

/**
 * Every account on the platform, as an administrator sees it, narrowed by the
 * server. Admin-only: a member gets 403, an authorization decision that keeps
 * the session and shows the refusal in the tab.
 */
export function billingAccountsQueryOptions(scope: AccountScope = EVERY_ACCOUNT) {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.accounts(scope),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/billing/accounts", {
          params: {
            query: {
              limit: PAGE_SIZE,
              cursor: pageParam,
              search: scope.search || undefined,
              role: scope.role ?? undefined,
              status: scope.status ?? undefined,
            },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
  });
}

export function updateUser(user: string, change: Schemas["UserUpdate"]) {
  return ok(api.PATCH("/v1/users/{user}", { params: { path: { user } }, body: change }));
}

export function setComplimentary(user: string, complimentary: boolean) {
  return ok(
    api.PUT("/v1/billing/accounts/{user}/complimentary", {
      params: { path: { user } },
      body: { complimentary },
    }),
  );
}
