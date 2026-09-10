import { Loader2 } from "lucide-react";

import { useSession } from "@/components/shared/AuthGate/session";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { platformRoleSchema, userStatusSchema, type BillingAccountAdmin } from "@/lib/api/schemas";
import { formatCostNanos } from "@/lib/money";

import {
  useAdminSettingsController,
  type AdminSettingsController,
  type PendingConfirmation,
} from "./controller";

const SELF_LOCKOUT = "You cannot change your own role or status. Ask another administrator.";

const ANY = "any";

/**
 * What narrows the list.
 *
 * Every control here is handed to the server. The list pages, so filtering the
 * rows already fetched would hide every match further down the walk and read as
 * the account simply not existing.
 */
function AccountFilterBar({ controller }: { controller: AdminSettingsController }) {
  const { filters } = controller;
  return (
    <div className="flex shrink-0 flex-col gap-2 border-b border-border px-4 py-3 sm:flex-row">
      <Input
        aria-label="Search accounts by name, email, or GitHub login"
        className="sm:flex-1"
        onChange={(event) => controller.setSearch(event.target.value)}
        placeholder="Search name, email, or GitHub login"
        value={filters.search}
      />
      <Select
        onValueChange={(value) =>
          controller.setRoleFilter(value === ANY ? null : platformRoleSchema.parse(value))
        }
        value={filters.role ?? ANY}
      >
        <SelectTrigger aria-label="Filter by role" className="sm:w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any role</SelectItem>
          <SelectItem value="administrator">Administrator</SelectItem>
          <SelectItem value="member">Member</SelectItem>
        </SelectContent>
      </Select>
      <Select
        onValueChange={(value) =>
          controller.setStatusFilter(value === ANY ? null : userStatusSchema.parse(value))
        }
        value={filters.status ?? ANY}
      >
        <SelectTrigger aria-label="Filter by status" className="sm:w-36">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any status</SelectItem>
          <SelectItem value="active">Active</SelectItem>
          <SelectItem value="disabled">Disabled</SelectItem>
        </SelectContent>
      </Select>
      {controller.narrowed ? (
        <Button onClick={controller.clearFilters} type="button" variant="ghost">
          Clear
        </Button>
      ) : null}
    </div>
  );
}

/** Every account on the platform, and what an administrator may do to each. */
export function AdminSettings() {
  const { user } = useSession();
  const controller = useAdminSettingsController({ actingUserId: user.id });

  return (
    <>
      <Panel
        title="Accounts"
        className="min-h-0 flex-1"
        contentClassName="flex flex-col overflow-hidden"
      >
        {controller.error ? (
          <p
            className="shrink-0 border-b border-border px-4 py-2 text-sm text-destructive"
            role="alert"
          >
            {controller.error.message}
          </p>
        ) : null}
        {controller.forbidden ? null : <AccountFilterBar controller={controller} />}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {controller.isLoading ? (
            <AccountTableSkeleton />
          ) : controller.forbidden ? (
            <PanelError message="Only an administrator can see accounts." />
          ) : controller.loadError ? (
            <PanelError message={controller.loadError.message} />
          ) : controller.accounts.length === 0 ? (
            <PanelEmpty
              message={controller.narrowed ? "No accounts match this search" : "No accounts yet"}
              className="min-h-full p-8"
            />
          ) : (
            <AccountTable controller={controller} />
          )}
        </div>
      </Panel>
      <ConfirmDialog controller={controller} />
    </>
  );
}

function AccountTable({ controller }: { controller: AdminSettingsController }) {
  return (
    <div>
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead>Person</TableHead>
            <TableHead>Role</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Billing</TableHead>
            <TableHead className="text-right">30-day usage</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {controller.accounts.map((account) => (
            <AccountRow key={account.user.id} account={account} controller={controller} />
          ))}
        </TableBody>
      </Table>
      <InfiniteScrollBoundary
        nextCursor={controller.nextCursor}
        loading={controller.loadingMore}
        error={controller.loadMoreError}
        onLoadMore={controller.loadMore}
        resourceLabel="accounts"
      />
    </div>
  );
}

function AccountRow({
  account,
  controller,
}: {
  account: BillingAccountAdmin;
  controller: AdminSettingsController;
}) {
  const { user } = account;
  const self = controller.isSelf(account);
  const pending = controller.pendingUserId === user.id;
  const disabled = controller.pendingUserId !== null;
  const active = user.status === "active";
  const complimentary = account.complimentary_since !== null;
  const secondary = user.email || user.github_login;

  return (
    <TableRow>
      <TableCell className="max-w-64">
        <div className="truncate text-sm font-medium">{user.display_name}</div>
        {secondary ? (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">{secondary}</div>
        ) : null}
      </TableCell>
      <TableCell>
        <Select
          value={user.role}
          disabled={disabled || self}
          onValueChange={(value) => controller.setRole(account, platformRoleSchema.parse(value))}
        >
          <SelectTrigger
            size="sm"
            className="w-40 text-foreground"
            aria-label={`Role for ${user.display_name}`}
            title={self ? SELF_LOCKOUT : undefined}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="administrator">Administrator</SelectItem>
            <SelectItem value="member">Member</SelectItem>
          </SelectContent>
        </Select>
      </TableCell>
      <TableCell>
        <StatusChip status={user.status} live={active} />
      </TableCell>
      <TableCell className="text-sm">
        <BillingCell account={account} />
      </TableCell>
      <TableCell className="text-right font-mono text-sm">
        {formatCostNanos(account.recent_cost_nanos, "USD")}
      </TableCell>
      <TableCell>
        <div className="flex items-center justify-end gap-1">
          {pending ? (
            <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label="Working" />
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            disabled={disabled}
            onClick={() => controller.toggleComplimentary(account)}
          >
            {complimentary ? "Revoke complimentary" : "Grant complimentary"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            className={active ? "text-destructive" : undefined}
            disabled={disabled || (self && active)}
            title={self && active ? SELF_LOCKOUT : undefined}
            onClick={() => controller.toggleStatus(account)}
          >
            {active ? "Disable" : "Enable"}
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

/**
 * What the account is held to, in one phrase.
 *
 * A waiver outranks the plan. The plan is still there and comes back when the
 * waiver goes, but while it stands nothing is owed, which is the fact an
 * administrator scanning the column wants.
 */
function BillingCell({ account }: { account: BillingAccountAdmin }) {
  const standing = account.complimentary_since
    ? "Complimentary"
    : account.plan
      ? capitalize(account.plan)
      : "No plan";
  return (
    <>
      <div>{standing}</div>
      {account.status === "past_due" ? (
        <div className="text-xs text-warning">Past due</div>
      ) : account.payment_method_on_file ? (
        <div className="text-xs text-muted-foreground">Card on file</div>
      ) : null}
    </>
  );
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function ConfirmDialog({ controller }: { controller: AdminSettingsController }) {
  const pending = controller.confirming;
  const busy = controller.pendingUserId !== null;
  return (
    <AlertDialog
      open={pending !== null}
      onOpenChange={(open) => (open ? undefined : controller.dismiss())}
    >
      {pending ? (
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{confirmTitle(pending)}</AlertDialogTitle>
            <AlertDialogDescription>{confirmDescription(pending)}</AlertDialogDescription>
          </AlertDialogHeader>
          {controller.error ? (
            <p className="text-sm text-destructive" role="alert">
              {controller.error.message}
            </p>
          ) : null}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
            <Button
              type="button"
              variant="destructive"
              disabled={busy}
              onClick={controller.confirm}
            >
              {busy ? <Loader2 className="animate-spin" /> : null}
              {confirmLabel(pending)}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      ) : null}
    </AlertDialog>
  );
}

function confirmTitle({ action, account }: PendingConfirmation): string {
  const name = account.user.display_name;
  switch (action) {
    case "demote":
      return `Make ${name} a member?`;
    case "disable":
      return `Disable ${name}?`;
    case "revoke":
      return `Revoke complimentary billing for ${name}?`;
  }
}

function confirmDescription({ action }: PendingConfirmation): string {
  switch (action) {
    case "demote":
      return "They lose access to platform administration, including this page.";
    case "disable":
      return "They can no longer sign in, and their tokens stop working. Running work is not stopped.";
    case "revoke":
      return "Usage from now on is billed to the account's plan and card again.";
  }
}

function confirmLabel({ action }: PendingConfirmation): string {
  switch (action) {
    case "demote":
      return "Make member";
    case "disable":
      return "Disable account";
    case "revoke":
      return "Revoke";
  }
}

function AccountTableSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {[0, 1, 2].map((item) => (
        <Skeleton key={item} className="h-9 w-full" />
      ))}
    </div>
  );
}
