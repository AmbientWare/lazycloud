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
  type BillingAccountAdminList,
  type PlatformRole,
  type User,
  type UserStatus,
} from "@/lib/api/schemas";
import { selectInfiniteList, type InfiniteListQueryData } from "@/lib/queries/infinite-list";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

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
export function billingAccountsQueryOptions() {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.accounts(),
    initialPageParam: "",
    queryFn: ({ pageParam }) => {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (pageParam) params.set("cursor", pageParam);
      return apiRequest(`${ACCOUNTS}?${params.toString()}`, billingAccountAdminListSchema);
    },
    getNextPageParam: nextAccountsCursor,
  });
}

/** A repeated cursor would page forever, so a server that returns one ends the list. */
function nextAccountsCursor(
  lastPage: BillingAccountAdminList,
  pages: BillingAccountAdminList[],
): string | undefined {
  if (!lastPage.next) return undefined;
  const cursorAlreadySeen = pages.slice(0, -1).some((page) => page.next === lastPage.next);
  return cursorAlreadySeen ? undefined : lastPage.next;
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
