import { useQuery } from "@tanstack/react-query";

import { useSession } from "@/components/shared/AuthGate/session";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import type { Workspace } from "@/lib/api/schemas";
import { billingSummaryQueryOptions } from "@/lib/queries/billing";
import { workspaceMembersQueryOptions } from "@/lib/queries/members";

export function WorkspaceMembersDialog({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const { user } = useSession();
  const members = useQuery(workspaceMembersQueryOptions(workspace.id, workspace.name));
  const owner = members.data?.data.some(
    (member) => member.user_id === user.id && member.role === "owner",
  );
  const billing = useQuery({ ...billingSummaryQueryOptions(), enabled: owner === true });

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{workspace.name} members</DialogTitle>
          <DialogDescription>People who can access this workspace.</DialogDescription>
        </DialogHeader>
        {owner && billing.data?.entitlements ? (
          <p className="border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
            {billing.data.usage.members}{" "}
            {billing.data.usage.members === 1 ? "distinct member" : "distinct members"} across this
            account
            {billing.data.entitlements.max_members === "unlimited"
              ? " · unlimited seats"
              : ` · ${billing.data.entitlements.max_members} allowed`}
          </p>
        ) : null}
        {members.isPending ? (
          <div className="space-y-2" aria-hidden="true">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : members.error ? (
          <p className="text-sm text-destructive" role="alert">
            {members.error.message}
          </p>
        ) : (
          <ul className="divide-y divide-border border-y border-border">
            {(members.data?.data ?? []).map((member) => (
              <li className="flex items-center justify-between gap-4 py-3" key={member.user_id}>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">{member.display_name}</span>
                  {member.email ? (
                    <span className="block truncate text-xs text-muted-foreground">
                      {member.email}
                    </span>
                  ) : null}
                </span>
                <span className="shrink-0 text-xs capitalize text-muted-foreground">
                  {member.role}
                </span>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
