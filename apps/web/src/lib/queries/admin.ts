import { infiniteQueryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import {
  billingAccountAdminListSchema,
  billingAccountAdminSchema,
  billingComplimentaryRequestSchema,
  userRoleRequestSchema,
  userSchema,
  userStatusRequestSchema,
  type BillingAccountAdmin,
  type PlatformRole,
  type User,
  type UserStatus,
} from "@/lib/api/schemas";
import {
  nextListCursor,
  selectInfiniteList,
  type InfiniteListQueryData,
} from "@/lib/queries/infinite-list";
import {
  accountQueryKeys,
  EVERY_ACCOUNT,
  type AdminAccountsKeyParts,
} from "@/lib/queries/workspace-keys";

const ACCOUNTS = "/api/v1/billing/accounts";
const USERS = "/api/v1/users";
const PAGE_SIZE = 50;

/**
 * Every account on the platform, as an administrator sees it.
 *
 * Admin-only on the server, which answers a member with 403. That is an
 * authorization decision rather than a dead token, so the client keeps the
 * session and the tab shows the refusal.
 */
export function billingAccountsQueryOptions(scope: AdminAccountsKeyParts = EVERY_ACCOUNT) {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.accounts(scope),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (pageParam) params.set("cursor", pageParam);
      // Narrowed by the server. The list pages, so filtering what arrived would
      // hide every match that had not been fetched yet.
      if (scope.search) params.set("search", scope.search);
      if (scope.role) params.set("role", scope.role);
      if (scope.status) params.set("status", scope.status);
      return apiRequest(`${ACCOUNTS}?${params.toString()}`, billingAccountAdminListSchema);
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

export function setUserRole(userId: string, role: PlatformRole): Promise<User> {
  const request = userRoleRequestSchema.parse({ role });
  return apiRequest(`${USERS}/${encodeURIComponent(userId)}/role`, userSchema, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export function setUserStatus(userId: string, status: UserStatus): Promise<User> {
  const request = userStatusRequestSchema.parse({ status });
  return apiRequest(`${USERS}/${encodeURIComponent(userId)}/status`, userSchema, {
    method: "PUT",
    body: JSON.stringify(request),
  });
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
