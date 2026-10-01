import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, apiRequest, ok, type Schemas } from "@/lib/api/client";
import {
  billingAccountAdminSchema,
  billingComplimentaryRequestSchema,
  type BillingAccountAdmin,
  type BillingAccountAdminList,
  type PlatformRole,
  type User,
  type UserStatus,
} from "@/lib/api/schemas";
import { viewUser } from "@/lib/api/views";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

const ACCOUNTS = "/api/v1/billing/accounts";
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
 *
 * Read from the users list until billing serves its account list; until then
 * every row carries no billing standing and no recent spend.
 */
export function billingAccountsQueryOptions(scope: AccountScope = EVERY_ACCOUNT) {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.accounts(scope),
    initialPageParam: "",
    queryFn: async ({ pageParam }): Promise<BillingAccountAdminList> => {
      // Narrowed by the server. The list pages, so filtering what arrived would
      // hide every match that had not been fetched yet.
      const page = await ok(
        api.GET("/v1/users", {
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
      return { data: page.users.map(unbilledAccount), next: page.next_cursor ?? "" };
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

export function setComplimentary(
  userId: string,
  complimentary: boolean,
): Promise<BillingAccountAdmin> {
  const request = billingComplimentaryRequestSchema.parse({ complimentary });
  return apiRequest(
    `${ACCOUNTS}/${encodeURIComponent(userId)}/complimentary`,
    billingAccountAdminSchema,
    { method: "PUT", body: JSON.stringify(request) },
  );
}

function unbilledAccount(user: Schemas["User"]): BillingAccountAdmin {
  return {
    user: viewUser(user),
    status: null,
    plan: null,
    payment_method_on_file: false,
    complimentary_since: null,
    recent_cost_nanos: 0,
    recent_cost_since: user.created_at,
  };
}
