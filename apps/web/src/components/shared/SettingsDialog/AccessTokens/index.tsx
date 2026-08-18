import { useState } from "react";
import { Check, Copy, Eye, EyeOff, Loader2, Plus, Trash2 } from "lucide-react";

import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelError } from "@/components/shared/PanelError";
import { StatusChip } from "@/components/shared/StatusChip";
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
import type { AuthToken } from "@/lib/api/schemas";
import { relativeTime } from "@/lib/format";

import {
  useAccessTokensController,
  type AccessTokensController,
  type IssuedToken,
} from "./controller";

type Expiry = "86400" | "604800" | "2592000" | "7776000" | "never";

const EXPIRY_VALUES: readonly Expiry[] = ["86400", "604800", "2592000", "7776000", "never"];

/** Every credential the account holds, in one list, because that is how they work. */
export function AccessTokens() {
  const controller = useAccessTokensController();

  return (
    <Panel
      title="Access tokens"
      action={
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
      }
      className="min-h-0 flex-1"
      contentClassName="flex flex-col overflow-hidden"
    >
      {controller.issued ? (
        <IssuedTokenNotice issued={controller.issued} onDismiss={controller.dismissIssued} />
      ) : controller.createMode !== "closed" ? (
        <CreateTokenForm controller={controller} />
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {controller.isLoading ? (
          <TokenTableSkeleton />
        ) : controller.loadError ? (
          <PanelError message="Access tokens could not be loaded. Try again shortly." />
        ) : controller.tokens.length === 0 ? (
          <div className="flex min-h-32 flex-col items-center justify-center gap-1 p-8 text-center">
            <p className="text-sm text-foreground">No tokens yet</p>
            <p className="text-xs text-muted-foreground">
              Create one to reach this account from the CLI, CI, or the API.
            </p>
          </div>
        ) : (
          <TokenTable controller={controller} tokens={controller.tokens} />
        )}
      </div>
    </Panel>
  );
}

function TokenTable({
  controller,
  tokens,
}: {
  controller: AccessTokensController;
  tokens: readonly AuthToken[];
}) {
  return (
    <div>
      <div
        className="micro-label sticky top-0 z-10 hidden h-9 grid-cols-12 items-center gap-3 border-b border-border bg-card px-3 lg:grid"
        aria-hidden="true"
      >
        <span className="col-span-4">Name</span>
        <span className="col-span-2">Status</span>
        <span className="col-span-2">Expires</span>
        <span className="col-span-2">Last used</span>
        <span className="col-span-2 text-right">Actions</span>
      </div>
      <ul aria-label="Access tokens">
        {tokens.map((token) => (
          <TokenRow key={token.id} token={token} controller={controller} />
        ))}
      </ul>
      <InfiniteScrollBoundary
        nextCursor={controller.nextCursor}
        loading={controller.loadingMore}
        error={controller.loadMoreError}
        onLoadMore={controller.loadMore}
        resourceLabel="access tokens"
      />
    </div>
  );
}

function TokenRow({ token, controller }: { token: AuthToken; controller: AccessTokensController }) {
  const active = token.status === "active";
  const owned = controller.actionTokenId === token.id;
  const confirming =
    owned && (controller.actionMode === "confirming" || controller.actionMode === "error");
  const running = owned && controller.actionMode === "running";
  const actionsDisabled = controller.isCommandPending || controller.createMode !== "closed";

  return (
    <li className="interactive-row grid grid-cols-2 gap-x-6 gap-y-4 border-b border-border px-4 py-3 last:border-b-0 lg:grid-cols-12 lg:items-center lg:gap-3 lg:px-3 lg:py-2">
      <div className="col-span-2 min-w-0 lg:col-span-4">
        <div className="truncate text-sm font-medium">{token.name}</div>
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
        <div className="micro-label mb-1 lg:hidden">Status</div>
        <StatusChip status={token.status} live={active} />
        {token.disabled_by_admin ? (
          <div className="mt-1 text-xs text-warning">Disabled by admin</div>
        ) : null}
      </div>
      <TokenTime label="Expires" value={token.expires_at} fallback="Never" />
      <TokenTime label="Last used" value={token.last_used_at} fallback="Never" />
      <div className="col-span-2 flex min-w-0 flex-col items-end justify-center gap-1 border-t border-border/70 pt-2 lg:col-span-2 lg:border-0 lg:pt-0">
        {confirming ? (
          <span className="flex items-center justify-end gap-1">
            <Button variant="destructive" size="sm" onClick={controller.confirmAction}>
              Delete
            </Button>
            <Button variant="ghost" size="sm" onClick={controller.cancelAction}>
              Keep
            </Button>
          </span>
        ) : running ? (
          <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label="Working" />
        ) : (
          <span className="flex items-center justify-end gap-1">
            <Button
              variant="ghost"
              size="icon"
              aria-label={`Delete ${token.name}`}
              title="Delete token"
              disabled={actionsDisabled}
              onClick={() => controller.beginAction(token)}
            >
              <Trash2 className="text-destructive" />
            </Button>
          </span>
        )}
        {controller.actionError && owned ? (
          <p className="text-right text-xs text-destructive" role="alert">
            {controller.actionError.message}
          </p>
        ) : null}
      </div>
    </li>
  );
}

function TokenTime({
  label,
  value,
  fallback,
}: {
  label: string;
  value: string | null;
  fallback: string;
}) {
  return (
    <div className="min-w-0 text-xs text-muted-foreground lg:col-span-2">
      <div className="micro-label mb-1 lg:hidden">{label}</div>
      {value ? (
        <time dateTime={value} title={new Date(value).toLocaleString()}>
          {relativeTime(value)}
        </time>
      ) : (
        fallback
      )}
    </div>
  );
}

/**
 * The one moment on this tab that cannot be repeated, so it is the one loud thing.
 *
 * The value stays masked until it is asked for, copying never requires revealing it,
 * and dismissing drops the only copy the browser holds.
 */
function IssuedTokenNotice({ issued, onDismiss }: { issued: IssuedToken; onDismiss: () => void }) {
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);

  return (
    <section
      aria-live="polite"
      className="shrink-0 border-b border-l-2 border-border border-l-brand bg-brand/[0.04] px-4 py-3"
    >
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h3 className="text-sm font-medium">Token created</h3>
        <span className="min-w-0 truncate text-xs text-muted-foreground">{issued.name}</span>
      </div>
      <p className="mt-0.5 text-[11px] text-muted-foreground">
        This is the only time the value is shown. Copy it now — dismissing this clears it from the
        browser. Store it on the machine that needs it with{" "}
        <code className="mono">lazycloud token set</code>.
      </p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <code className="mono min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-md border border-input bg-background px-3 py-2 text-xs">
          {revealed ? issued.secret : `${issued.prefix}${"•".repeat(24)}`}
        </code>
        <Button
          variant="outline"
          size="icon"
          aria-label={revealed ? "Hide token value" : "Reveal token value"}
          title={revealed ? "Hide token value" : "Reveal token value"}
          onClick={() => setRevealed((value) => !value)}
        >
          {revealed ? <EyeOff /> : <Eye />}
        </Button>
        <Button
          variant="outline"
          size="icon"
          aria-label="Copy token value"
          title="Copy token value"
          onClick={() => {
            void navigator.clipboard.writeText(issued.secret).then(() => setCopied(true));
          }}
        >
          {copied ? <Check className="text-positive" /> : <Copy />}
        </Button>
        <Button size="sm" onClick={onDismiss}>
          Done
        </Button>
      </div>
    </section>
  );
}

function CreateTokenForm({ controller }: { controller: AccessTokensController }) {
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState<Expiry>("2592000");
  const creating = controller.createMode === "creating";

  return (
    <form
      className="grid shrink-0 gap-3 border-b border-border p-4 sm:grid-cols-[minmax(180px,1fr)_170px_auto] sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        controller.create({
          name,
          expiresInSeconds: expiry === "never" ? null : Number(expiry),
        });
      }}
    >
      <label className="grid gap-1.5">
        <span className="micro-label">Name</span>
        <Input
          autoFocus
          value={name}
          disabled={creating}
          onChange={(event) => setName(event.target.value)}
          placeholder="ci-deploy"
          className="h-8 bg-background"
        />
      </label>
      <label className="grid gap-1.5">
        <span className="micro-label">Expires</span>
        <Select
          value={expiry}
          disabled={creating}
          onValueChange={(value) => {
            if (isExpiry(value)) setExpiry(value);
          }}
        >
          <SelectTrigger size="sm" className="w-full text-foreground" aria-label="Token expiry">
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
          disabled={creating}
          onClick={controller.cancelCreate}
        >
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={creating || !name.trim()}>
          {creating ? <Loader2 className="animate-spin" /> : null}
          Create token
        </Button>
      </div>
      {controller.createError ? (
        <p className="text-xs text-destructive sm:col-span-3" role="alert">
          {controller.createError.message}
        </p>
      ) : null}
    </form>
  );
}

function isExpiry(value: string): value is Expiry {
  return (EXPIRY_VALUES as readonly string[]).includes(value);
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
