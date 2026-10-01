import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type {
  BillingAccountAdmin,
  BillingAccountAdminList,
  PlatformRole,
  User,
  UserStatus,
} from "@/lib/api/schemas";
import { viewUser } from "@/lib/api/views";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const PAGE_SIZE = 50;

type AccountScope = {
  search: string;
  role: PlatformRole | null;
  status: UserStatus | null;
};

const EVERY_ACCOUNT: AccountScope = { search: "", role: null, status: null };

/**
 * Every account on the platform, as an administrator sees it.
 *
 * Admin-only on the server, which answers a member with 403. That is an
 * authorization decision rather than a dead token, so the client keeps the
 * session and the tab shows the refusal.
 */
export function billingAccountsQueryOptions(scope: AccountScope = EVERY_ACCOUNT) {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.accounts(scope),
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<BillingAccountAdminList> => {
      // Narrowed by the server. The list pages, so filtering what arrived would
      // hide every match that had not been fetched yet.
      const page = await ok(
        api.GET("/v1/billing/accounts", {
          params: {
            query: {
              limit: PAGE_SIZE,
              cursor: pageParam || undefined,
              search: scope.search || undefined,
              role: scope.role ?? undefined,
              status: scope.status ?? undefined,
            },
          },
        }),
      );
      return { data: page.accounts.map(viewAccount), next: page.next_cursor ?? "" };
    },
    getNextPageParam: nextListCursor,
  });
}

export function selectBillingAccountList(
  data: InfiniteListQueryData<BillingAccountAdmin> | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectInfiniteList(data, hasNextPage, (account) => account.user.id);
}

export async function setUserRole(user: string, role: PlatformRole): Promise<User> {
  return viewUser(
    await ok(api.PUT("/v1/users/{user}/role", { params: { path: { user } }, body: { role } })),
  );
}

export async function setUserStatus(user: string, status: UserStatus): Promise<User> {
  return viewUser(
    await ok(api.PUT("/v1/users/{user}/status", { params: { path: { user } }, body: { status } })),
  );
}

export async function setComplimentary(
  user: string,
  complimentary: boolean,
): Promise<BillingAccountAdmin> {
  return viewAccount(
    await ok(
      api.PUT("/v1/billing/accounts/{user}/complimentary", {
        params: { path: { user } },
        body: { complimentary },
      }),
    ),
  );
}

function viewAccount(account: Schemas["BillingAccountAdmin"]): BillingAccountAdmin {
  return {
    user: viewUser(account.user),
    status: account.status ?? null,
    plan: account.plan ?? null,
    payment_method_on_file: account.payment_method_on_file,
    complimentary_since: account.complimentary_since ?? null,
    recent_cost_nanos: account.recent_cost_nanos,
    recent_cost_since: account.recent_cost_since,
  };
}
