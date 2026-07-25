import { useState } from "react";
import { Check, Copy, Loader2, Plus, Power, Trash2 } from "lucide-react";

import { CliHint } from "@/components/shared/CliHint";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { AuthToken } from "@/lib/api/schemas";
import type { CreateTokenInput } from "@/lib/queries/tokens";
import { relativeTime } from "@/lib/format";

import {
  isManagedToken,
  useWorkspaceTokensController,
  type WorkspaceTokensController,
} from "./controller";

type AccessLevel = "read" | "write";
type Expiry = "86400" | "604800" | "2592000" | "7776000" | "never";

export function WorkspaceTokens({ workspaceId }: { workspaceId: string }) {
  return <WorkspaceTokensForWorkspace key={workspaceId} workspaceId={workspaceId} />;
}

function WorkspaceTokensForWorkspace({ workspaceId }: { workspaceId: string }) {
  const controller = useWorkspaceTokensController(workspaceId);
  const [showManaged, setShowManaged] = useState(false);
  const managedCount = controller.tokens.filter(isManagedToken).length;
  const visibleTokens = showManaged
    ? controller.tokens
    : controller.tokens.filter((token) => !isManagedToken(token));

  return (
    <Panel
      title="Access tokens"
      description="Scoped credentials for CLI, automation, and API access"
      action={
        <div className="flex flex-wrap items-center justify-end gap-2">
          {managedCount > 0 ? (
            <Button
              variant="ghost"
              size="sm"
              disabled={controller.isCommandPending}
              onClick={() => setShowManaged((value) => !value)}
            >
              {showManaged
                ? "Hide managed credentials"
                : `Show managed credentials (${managedCount})`}
            </Button>
          ) : null}
          <Button
            size="sm"
            onClick={controller.beginCreate}
            disabled={
              controller.createMode !== "closed" ||
              controller.actionMode !== "idle" ||
              controller.isCommandPending
            }
          >
            <Plus />
            Create token
          </Button>
        </div>
      }
      className="min-h-[18rem] lg:col-span-full lg:row-start-2 lg:min-h-0"
      headerClassName="flex-wrap"
      contentClassName="flex flex-col overflow-visible lg:overflow-hidden"
    >
      {controller.createMode !== "closed" ? (
        <CreateTokenForm controller={controller} />
      ) : null}

      <div className="min-h-0 flex-1 lg:overflow-y-auto">
        {controller.isLoading ? (
          <TokenTableSkeleton />
        ) : controller.loadError ? (
          <div className="p-4 text-sm text-destructive" role="alert">
            Access tokens could not be loaded. Try again shortly.
          </div>
        ) : visibleTokens.length === 0 ? (
          <div className="flex min-h-32 flex-col items-center justify-center gap-3 p-8 text-sm text-muted-foreground">
            <span>
              {managedCount > 0 ? "No user-managed tokens" : "No access tokens"}
            </span>
            <CliHint command="lazycloud token create dashboard" />
          </div>
        ) : (
          <TokenTable
            controller={controller}
            tokens={visibleTokens}
          />
        )}
      </div>
    </Panel>
  );
}

function TokenTable({
  controller,
  tokens,
}: {
  controller: WorkspaceTokensController;
  tokens: readonly AuthToken[];
}) {
  return (
    <div>
      <div
        className="micro-label hidden h-9 grid-cols-12 items-center gap-3 border-b border-border px-3 lg:grid"
        aria-hidden="true"
      >
        <span className="col-span-3">Name</span>
        <span className="col-span-2">Access</span>
        <span className="col-span-2">Status</span>
        <span className="col-span-2">Expires</span>
        <span className="col-span-2">Last used</span>
        <span className="text-right">Actions</span>
      </div>
      <ul aria-label="Access tokens">
        {tokens.map((token) => (
          <TokenRow key={token.id} token={token} controller={controller} />
        ))}
      </ul>
    </div>
  );
}

function TokenRow({
  token,
  controller,
}: {
  token: AuthToken;
  controller: WorkspaceTokensController;
}) {
  const managed = isManagedToken(token);
  const active = token.status === "active";
  const actionOwned = controller.actionTokenId === token.id;
  const confirmingDelete =
    actionOwned &&
    controller.actionKind === "delete" &&
    (controller.actionMode === "confirming" ||
      controller.actionMode === "deleting" ||
      controller.actionMode === "error");
  const toggling = actionOwned && controller.actionMode === "toggling";
  const deleting = actionOwned && controller.actionMode === "deleting";
  const actionFailed = actionOwned && controller.actionMode === "error";
  const actionsDisabled =
    controller.isCommandPending || controller.createMode !== "closed";

  return (
    <li className="interactive-row grid grid-cols-2 gap-x-6 gap-y-4 border-b border-border px-4 py-3 last:border-b-0 lg:grid-cols-12 lg:items-center lg:gap-3 lg:px-3 lg:py-2">
      <div className="col-span-2 min-w-0 lg:col-span-3">
        <div className="truncate font-medium">{token.name}</div>
        <div className="mt-0.5 flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
          <code className="mono shrink-0">{token.prefix}...</code>
          <span aria-hidden="true">/</span>
          <time
            dateTime={token.created_at}
            title={new Date(token.created_at).toLocaleString()}
            className="truncate"
          >
            Created {relativeTime(token.created_at)}
          </time>
        </div>
      </div>
      <div className="min-w-0 lg:col-span-2">
        <div className="micro-label mb-1 lg:hidden">Access</div>
        <div className="text-sm">{formatScopes(token.scopes)}</div>
        {managed ? (
          <div className="truncate text-xs text-muted-foreground">{token.kind}</div>
        ) : null}
      </div>
      <div className="min-w-0 lg:col-span-2">
        <div className="micro-label mb-1 lg:hidden">Status</div>
        <StatusChip status={token.status} live={active} />
        {token.disabled_by_admin ? (
          <div className="mt-1 text-xs text-warning">Disabled by admin</div>
        ) : null}
      </div>
      <div className="min-w-0 text-xs text-muted-foreground lg:col-span-2">
        <div className="micro-label mb-1 lg:hidden">Expires</div>
        {token.expires_at ? (
          <time
            dateTime={token.expires_at}
            title={new Date(token.expires_at).toLocaleString()}
          >
            {relativeTime(token.expires_at)}
          </time>
        ) : (
          "Never"
        )}
      </div>
      <div className="min-w-0 text-xs text-muted-foreground lg:col-span-2">
        <div className="micro-label mb-1 lg:hidden">Last used</div>
        {token.last_used_at ? (
          <time
            dateTime={token.last_used_at}
            title={new Date(token.last_used_at).toLocaleString()}
          >
            {relativeTime(token.last_used_at)}
          </time>
        ) : (
          "Never"
        )}
      </div>
      <div className="col-span-2 flex min-w-0 items-center justify-end border-t border-border/70 pt-2 text-right lg:col-span-1 lg:block lg:border-0 lg:pt-0">
        {managed ? (
          <span className="text-xs text-muted-foreground">Managed</span>
        ) : confirmingDelete ? (
          <span className="flex items-center justify-end gap-1">
            <Button
              variant="destructive"
              size="sm"
              disabled={deleting}
              onClick={() => controller.deleteConfirmed(token)}
            >
              Delete
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={deleting}
              onClick={controller.cancelDelete}
            >
              Keep
            </Button>
          </span>
        ) : (
          <span className="flex items-center justify-end gap-1">
            <Button
              variant="ghost"
              size="icon"
              aria-label={active ? "Disable token" : "Enable token"}
              title={active ? "Disable token" : "Enable token"}
              disabled={actionsDisabled}
              onClick={() => controller.toggle(token)}
            >
              {toggling ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Power className={active ? undefined : "text-muted-foreground"} />
              )}
            </Button>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Delete token"
              title="Delete token"
              disabled={actionsDisabled}
              onClick={() => controller.beginDelete(token)}
            >
              <Trash2 className="text-destructive" />
            </Button>
          </span>
        )}
        {actionFailed ? (
          <div className="ml-2 text-xs text-destructive lg:ml-0 lg:mt-1" role="alert">
            {controller.actionError?.message ?? "Token action failed"}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function CreateTokenForm({
  controller,
}: {
  controller: WorkspaceTokensController;
}) {
  const [name, setName] = useState("");
  const [access, setAccess] = useState<AccessLevel>("read");
  const [expiry, setExpiry] = useState<Expiry>("2592000");
  const [copied, setCopied] = useState(false);

  if (controller.createMode === "issued" && controller.issuedSecret) {
    return (
      <div className="shrink-0 border-b border-border p-4" aria-live="polite">
        <div className="mb-2 text-sm font-medium">Token created</div>
        <p className="mb-3 text-xs text-muted-foreground">
          Copy it now. The value is not available after this view is closed.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <code className="mono min-w-0 flex-1 overflow-x-auto rounded-md border border-input bg-muted/30 px-3 py-2 text-xs">
            {controller.issuedSecret}
          </code>
          <Button
            variant="outline"
            size="icon"
            aria-label="Copy token"
            title="Copy token"
            onClick={() => {
              void navigator.clipboard
                .writeText(controller.issuedSecret ?? "")
                .then(() => setCopied(true));
            }}
          >
            {copied ? <Check className="text-positive" /> : <Copy />}
          </Button>
          <Button size="sm" onClick={controller.acknowledgeIssued}>
            Done
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      className="grid shrink-0 gap-3 border-b border-border p-4 sm:grid-cols-[minmax(180px,1fr)_170px_170px_auto] sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        controller.create(tokenInput(name, access, expiry));
      }}
    >
      <label className="grid gap-1.5 text-xs text-muted-foreground">
        Name
        <input
          autoFocus
          value={name}
          disabled={controller.createMode === "creating"}
          onChange={(event) => setName(event.target.value)}
          placeholder="ci-deploy"
          className="h-8 min-w-0 rounded-md border border-input bg-background px-2.5 text-sm text-foreground outline-none focus:border-ring"
        />
      </label>
      <label className="grid gap-1.5 text-xs text-muted-foreground">
        Access
        <Select
          value={access}
          disabled={controller.createMode === "creating"}
          onValueChange={(value) => {
            if (value === "read" || value === "write") setAccess(value);
          }}
        >
          <SelectTrigger
            size="sm"
            className="w-full text-foreground"
            aria-label="Token access"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="read">Read only</SelectItem>
            <SelectItem value="write">Read and write</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <label className="grid gap-1.5 text-xs text-muted-foreground">
        Expires
        <Select
          value={expiry}
          disabled={controller.createMode === "creating"}
          onValueChange={(value) => {
            if (isExpiry(value)) setExpiry(value);
          }}
        >
          <SelectTrigger
            size="sm"
            className="w-full text-foreground"
            aria-label="Token expiry"
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="86400">1 day</SelectItem>
            <SelectItem value="604800">7 days</SelectItem>
            <SelectItem value="2592000">30 days</SelectItem>
            <SelectItem value="7776000">90 days</SelectItem>
            <SelectItem value="never">Never</SelectItem>
          </SelectContent>
        </Select>
      </label>
      <div className="flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={controller.createMode === "creating"}
          onClick={controller.cancelCreate}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={controller.createMode === "creating" || !name.trim()}
        >
          {controller.createMode === "creating" ? (
            <Loader2 className="animate-spin" />
          ) : (
            "Create"
          )}
        </Button>
      </div>
      {controller.createError ? (
        <div className="text-xs text-destructive sm:col-span-4" role="alert">
          {controller.createError.message}
        </div>
      ) : null}
    </form>
  );
}

function isExpiry(value: string): value is Expiry {
  return ["86400", "604800", "2592000", "7776000", "never"].includes(
    value,
  );
}

function tokenInput(
  name: string,
  access: AccessLevel,
  expiry: Expiry,
): CreateTokenInput {
  return {
    name,
    scopes: access === "write" ? ["read", "write"] : ["read"],
    expiresInSeconds: expiry === "never" ? null : Number(expiry),
  };
}

function formatScopes(scopes: string[]): string {
  if (scopes.includes("*")) return "Full access";
  const visible = scopes.filter(
    (scope) => scope === "read" || scope === "write",
  );
  return visible.length ? visible.join(" + ") : "Custom";
}

function TokenTableSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {[0, 1, 2].map((item) => (
        <Skeleton key={item} className="h-9 w-full" />
      ))}
    </div>
  );
}
