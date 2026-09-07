import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, KeyRound, Loader2, Pencil, Trash2 } from "lucide-react";

import { CopyButton } from "@/components/shared/CopyButton";
import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { FormField } from "@/components/shared/FormField";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  createSecret,
  deleteSecret,
  revealSecretValue,
  secretsQueryOptions,
  updateSecretValue,
} from "@/lib/queries/storage";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import type { SecretMasked } from "@/lib/api/schemas";
import { ResourceWorkloadLinks } from "./ResourceWorkloadLinks";

/**
 * Collection reads stay masked. Cleartext is fetched only after an explicit
 * reveal action and lives in this row's local state until it is hidden again.
 */
export function SecretsTab({
  workspaceId,
  workspaceName,
  creating,
  onCreatingChange,
}: {
  workspaceId: string;
  workspaceName: string;
  creating: boolean;
  onCreatingChange: (open: boolean) => void;
}) {
  const query = useQuery(secretsQueryOptions(workspaceId));
  const [editing, setEditing] = useState<string | null>(null);

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:min-h-0">
      <div className="min-h-[24rem] flex-1 divide-y divide-border overflow-visible lg:min-h-0 lg:overflow-y-auto">
        <div
          aria-hidden="true"
          data-secret-table-header=""
          className="hidden min-h-11 grid-cols-[minmax(11rem,0.9fr)_minmax(14rem,1.35fr)_auto] items-center gap-4 px-4 py-2 text-sm font-medium text-foreground sm:grid"
        >
          <span>Secret</span>
          <span>Value</span>
          <span className="text-right">Actions</span>
        </div>
        {creating ? (
          <SecretForm
            workspaceId={workspaceId}
            mode="create"
            onDone={() => onCreatingChange(false)}
            onCancel={() => onCreatingChange(false)}
          />
        ) : null}
        {query.isError && query.data ? (
          <ApiErrorNotice
            compact
            error={query.error}
            title="Secrets could not be refreshed"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : null}
        {query.isPending ? (
          <SecretsSkeleton />
        ) : query.isError && !query.data ? (
          <ApiErrorNotice
            error={query.error}
            title="Secrets could not be loaded"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : query.data?.secrets.length === 0 && !creating ? (
          <PanelEmpty message="No secrets. Create one to inject into a workload." className="p-6" />
        ) : (
          query.data?.secrets.map((secret) =>
            editing === secret.name ? (
              <SecretForm
                key={secret.name}
                workspaceId={workspaceId}
                mode="update"
                name={secret.name}
                onDone={() => setEditing(null)}
                onCancel={() => setEditing(null)}
              />
            ) : (
              <SecretRow
                key={secret.name}
                workspaceId={workspaceId}
                workspaceName={workspaceName}
                secret={secret}
                onEdit={() => setEditing(secret.name)}
              />
            ),
          )
        )}
      </div>
    </div>
  );
}

function SecretsSkeleton() {
  return (
    <div aria-hidden="true">
      {Array.from({ length: 3 }, (_, index) => (
        <div
          key={index}
          className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-4 py-3 sm:grid-cols-[minmax(11rem,0.9fr)_minmax(14rem,1.35fr)_auto] sm:gap-4"
        >
          <div className="space-y-2">
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-3 w-24" />
          </div>
          <Skeleton className="col-span-2 h-9 w-full sm:col-span-1" />
          <Skeleton className="col-start-2 row-start-1 h-7 w-16 sm:col-auto sm:row-auto" />
        </div>
      ))}
    </div>
  );
}

function SecretRow({
  workspaceId,
  workspaceName,
  secret,
  onEdit,
}: {
  workspaceId: string;
  workspaceName: string;
  secret: SecretMasked;
  onEdit: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [revealedValue, setRevealedValue] = useState<string | null>(null);
  const [revealing, setRevealing] = useState(false);
  const [revealError, setRevealError] = useState<string | null>(null);
  const revealRequest = useRef<AbortController | null>(null);
  useEffect(() => () => revealRequest.current?.abort(), []);

  const clearReveal = () => {
    revealRequest.current?.abort();
    revealRequest.current = null;
    setRevealedValue(null);
    setRevealing(false);
    setRevealError(null);
  };
  const remove = useMutation({
    mutationFn: () => deleteSecret(workspaceId, secret.name),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.storage.secrets(workspaceId),
      }),
  });

  const toggleReveal = async () => {
    if (revealedValue !== null || revealing) {
      clearReveal();
      return;
    }
    if (confirming || remove.isPending) return;
    const request = new AbortController();
    revealRequest.current = request;
    setRevealing(true);
    setRevealError(null);
    try {
      const value = await revealSecretValue(workspaceId, secret.name, request.signal);
      if (!request.signal.aborted) setRevealedValue(value);
    } catch (error) {
      if (!request.signal.aborted)
        setRevealError(error instanceof Error ? error.message : "Unable to reveal secret");
    } finally {
      if (revealRequest.current === request) {
        revealRequest.current = null;
        setRevealing(false);
      }
    }
  };

  const beginDelete = () => {
    clearReveal();
    setConfirming(true);
  };

  return (
    <div
      aria-label={`Secret ${secret.name}`}
      className="interactive-row group grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-3 px-4 py-3 sm:grid-cols-[minmax(11rem,0.9fr)_minmax(14rem,1.35fr)_auto] sm:gap-x-4"
    >
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <KeyRound className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="mono truncate text-[13px] font-medium text-foreground">
            {secret.name}
          </span>
        </div>
        <p className="mt-1 pl-5.5 text-[11px] text-muted-foreground">
          Rotated <LiveRelativeTime value={secret.updated_at ?? secret.created_at ?? undefined} />
        </p>
        <div className="mt-1 pl-5.5">
          <ResourceWorkloadLinks workspaceName={workspaceName} workloads={secret.workloads} />
        </div>
      </div>

      <div className="col-span-2 flex h-9 min-w-0 items-center rounded-md border border-input bg-background/70 pl-3 shadow-xs transition-colors focus-within:border-ring sm:col-span-1">
        <code aria-live="polite" className="mono min-w-0 flex-1 truncate text-xs text-foreground">
          {revealedValue ?? "********"}
        </code>
        <CopyButton
          value={revealedValue ?? ""}
          label={`secret ${secret.name}`}
          disabled={revealedValue === null}
          className={revealedValue === null ? "invisible size-7 shrink-0" : "size-7 shrink-0"}
        />
        <Button
          variant="ghost"
          size="icon"
          className="mr-1 size-7 shrink-0"
          disabled={confirming || remove.isPending}
          onClick={() => void toggleReveal()}
          aria-label={`${revealing ? "Cancel revealing" : revealedValue !== null ? "Hide" : "Reveal"} secret ${secret.name}`}
          title={
            revealing
              ? "Cancel revealing"
              : revealedValue !== null
                ? "Hide secret"
                : "Reveal secret"
          }
        >
          {revealing ? (
            <Loader2 className="animate-spin" />
          ) : revealedValue !== null ? (
            <EyeOff />
          ) : (
            <Eye />
          )}
        </Button>
      </div>

      {confirming ? (
        <div className="col-start-2 row-start-1 flex items-center justify-end gap-1 sm:col-auto sm:row-auto">
          <Button
            variant="destructive"
            size="sm"
            disabled={remove.isPending}
            pending={remove.isPending}
            onClick={() => remove.mutate()}
          >
            Delete
          </Button>
          <Button
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => setConfirming(false)}
          >
            Keep
          </Button>
        </div>
      ) : (
        <div className="col-start-2 row-start-1 flex items-center justify-end gap-1 sm:col-auto sm:row-auto">
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={() => {
              clearReveal();
              onEdit();
            }}
            aria-label={`Rotate secret ${secret.name}`}
            title="Rotate secret"
          >
            <Pencil />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            className="size-7"
            onClick={beginDelete}
            aria-label={`Delete secret ${secret.name}`}
            title="Delete secret"
          >
            <Trash2 />
          </Button>
        </div>
      )}
      {remove.isError ? (
        <p role="alert" className="col-span-2 text-right text-xs text-destructive sm:col-span-3">
          {remove.error.message}
        </p>
      ) : null}
      {revealError ? (
        <p role="alert" className="col-span-2 text-right text-xs text-destructive sm:col-span-3">
          {revealError}
        </p>
      ) : null}
    </div>
  );
}

function SecretForm({
  workspaceId,
  mode,
  name: fixedName,
  onDone,
  onCancel,
}: {
  workspaceId: string;
  mode: "create" | "update";
  name?: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(fixedName ?? "");
  const [value, setValue] = useState("");
  const mutation = useMutation({
    mutationFn: async () => {
      if (mode === "create") {
        await createSecret(workspaceId, name.trim(), value);
      } else {
        await updateSecretValue(workspaceId, fixedName ?? name, value);
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.storage.secrets(workspaceId),
      });
      onDone();
    },
  });

  return (
    <form
      className="grid gap-3 bg-muted/25 px-4 py-4 sm:grid-cols-[minmax(11rem,0.9fr)_minmax(14rem,1.35fr)_auto] sm:items-end sm:gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        mutation.mutate();
      }}
    >
      <FormField
        label="Secret name"
        autoFocus={mode === "create"}
        value={name}
        disabled={mode === "update" || mutation.isPending}
        onChange={(event) => setName(event.target.value)}
        placeholder="SECRET_NAME"
        className="mono h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition-colors focus:border-ring disabled:bg-muted/40 disabled:text-muted-foreground"
      />
      <FormField
        label={mode === "create" ? "Value" : "New value"}
        disabled={mutation.isPending}
        autoFocus={mode === "update"}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        type="password"
        className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition-colors focus:border-ring"
      />
      <div className="flex items-center justify-end gap-2">
        <Button
          type="submit"
          size="sm"
          disabled={mutation.isPending || !value || (mode === "create" && !name.trim())}
          pending={mutation.isPending}
        >
          {mode === "create" ? "Create" : "Rotate"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={mutation.isPending}
          onClick={onCancel}
        >
          Cancel
        </Button>
      </div>
      {mutation.isError ? (
        <span className="text-xs text-destructive sm:col-span-3 sm:text-right">
          {mutation.error.message}
        </span>
      ) : null}
    </form>
  );
}
