import { useState } from "react";
import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
} from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import type {
  BillingAccountAdmin,
  BillingAccountAdminList,
  PlatformRole,
  User,
} from "@/lib/api/schemas";
import {
  billingAccountsQueryOptions,
  selectBillingAccountList,
  setComplimentary,
  setUserRole,
  setUserStatus,
} from "@/lib/queries/admin";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";

type AccountPages = InfiniteData<BillingAccountAdminList, string>;

/**
 * The three things an administrator can do to an account that are worth a
 * second look before they happen. Each takes something away, and two of them
 * can lock a person out.
 */
export type ConfirmedAction = "demote" | "disable" | "revoke";

export type PendingConfirmation = {
  action: ConfirmedAction;
  account: BillingAccountAdmin;
};

type Command =
  | { kind: "role"; userId: string; role: PlatformRole }
  | { kind: "status"; userId: string; status: User["status"] }
  | { kind: "complimentary"; userId: string; complimentary: boolean };

type CommandResult = { user: User } | { account: BillingAccountAdmin };

export type AdminSettingsController = {
  accounts: readonly BillingAccountAdmin[];
  isLoading: boolean;
  loadError: Error | null;
  forbidden: boolean;
  nextCursor: string | undefined;
  loadingMore: boolean;
  loadMoreError: boolean;
  loadMore: () => void;
  /** Whether the row is the signed-in administrator's own, which the UI may not demote or disable. */
  isSelf: (account: BillingAccountAdmin) => boolean;
  setRole: (account: BillingAccountAdmin, role: PlatformRole) => void;
  toggleStatus: (account: BillingAccountAdmin) => void;
  toggleComplimentary: (account: BillingAccountAdmin) => void;
  confirming: PendingConfirmation | null;
  confirm: () => void;
  dismiss: () => void;
  /** The account a request is in flight for, or null. */
  pendingUserId: string | null;
  error: Error | null;
};

/**
 * Every account on the platform, and what an administrator may change about one.
 *
 * `actingUserId` is a parameter rather than a read of the session so the rule
 * that keeps an administrator from locking themself out is checked here, where
 * it can be proven, and not in a rendered control.
 */
export function useAdminSettingsController({
  actingUserId,
}: {
  actingUserId: string;
}): AdminSettingsController {
  const queryClient = useQueryClient();
  const query = useInfiniteQuery(billingAccountsQueryOptions());
  const list = selectBillingAccountList(query.data, query.hasNextPage);
  const [confirming, setConfirming] = useState<PendingConfirmation | null>(null);

  const command = useMutation({
    mutationFn: runCommand,
    onSuccess: (result, sent) => {
      // The answer is the row as it now stands, so it is written into the list
      // the tab reads rather than refetching every page.
      patchAccount(queryClient, sent.userId, (row) =>
        "account" in result ? result.account : { ...row, user: result.user },
      );
      setConfirming(null);
    },
  });

  const isSelf = (account: BillingAccountAdmin) => account.user.id === actingUserId;

  const run = (next: Command) => {
    if (command.isPending) return;
    command.mutate(next);
  };

  const setRole = (account: BillingAccountAdmin, role: PlatformRole) => {
    if (command.isPending || account.user.role === role) return;
    if (role === "member") {
      if (isSelf(account)) return;
      setConfirming({ action: "demote", account });
      return;
    }
    run({ kind: "role", userId: account.user.id, role });
  };

  const toggleStatus = (account: BillingAccountAdmin) => {
    if (command.isPending) return;
    if (account.user.status === "active") {
      if (isSelf(account)) return;
      setConfirming({ action: "disable", account });
      return;
    }
    run({ kind: "status", userId: account.user.id, status: "active" });
  };

  const toggleComplimentary = (account: BillingAccountAdmin) => {
    if (command.isPending) return;
    if (account.complimentary_since) {
      setConfirming({ action: "revoke", account });
      return;
    }
    run({ kind: "complimentary", userId: account.user.id, complimentary: true });
  };

  const confirm = () => {
    if (!confirming || command.isPending) return;
    const userId = confirming.account.user.id;
    switch (confirming.action) {
      case "demote":
        run({ kind: "role", userId, role: "member" });
        return;
      case "disable":
        run({ kind: "status", userId, status: "disabled" });
        return;
      case "revoke":
        run({ kind: "complimentary", userId, complimentary: false });
        return;
    }
  };

  return {
    accounts: list.items,
    isLoading: query.isPending,
    loadError: query.error,
    forbidden: query.error instanceof ApiError && query.error.status === 403,
    nextCursor: list.nextCursor,
    loadingMore: query.isFetchingNextPage,
    loadMoreError: query.isFetchNextPageError,
    loadMore: () => void query.fetchNextPage(),
    isSelf,
    setRole,
    toggleStatus,
    toggleComplimentary,
    confirming,
    confirm,
    dismiss: () => {
      if (command.isPending) return;
      setConfirming(null);
    },
    pendingUserId: command.isPending ? command.variables.userId : null,
    error: command.error,
  };
}

async function runCommand(sent: Command): Promise<CommandResult> {
  switch (sent.kind) {
    case "role":
      return { user: await setUserRole(sent.userId, sent.role) };
    case "status":
      return { user: await setUserStatus(sent.userId, sent.status) };
    case "complimentary":
      return { account: await setComplimentary(sent.userId, sent.complimentary) };
  }
}

function patchAccount(
  queryClient: QueryClient,
  userId: string,
  patch: (row: BillingAccountAdmin) => BillingAccountAdmin,
): void {
  queryClient.setQueryData<AccountPages>(accountQueryKeys.admin.accounts(), (current) =>
    current
      ? {
          ...current,
          pages: current.pages.map((page) => ({
            ...page,
            data: page.data.map((row) => (row.user.id === userId ? patch(row) : row)),
          })),
        }
      : current,
  );
}
