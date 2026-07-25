import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSession } from "@/components/shared/AuthGate/session";
import { ApiError } from "@/lib/api/client";
import type { DeviceCode } from "@/lib/api/schemas";
import {
  approveDeviceCodeMutationOptions,
  denyDeviceCodeMutationOptions,
  deviceCodeQueryOptions,
} from "@/lib/queries/auth";
import { relativeTime } from "@/lib/format";

export const Route = createFileRoute("/activate")({
  validateSearch: (search: Record<string, unknown>): { code: string } => ({
    code: typeof search.code === "string" ? search.code : "",
  }),
  component: ActivatePage,
});

/** Normalize a typed user code to the canonical XXXX-XXXX form for lookup. */
function normalizeUserCode(value: string): string {
  const compact = value.toUpperCase().replace(/[^A-Z0-9]/g, "");
  return compact.length === 8 ? `${compact.slice(0, 4)}-${compact.slice(4)}` : compact;
}

function ActivatePage() {
  const { code } = Route.useSearch();
  const userCode = normalizeUserCode(code);

  return (
    <main className="flex min-h-screen items-center justify-center bg-background p-4">
      <section className="panel w-full max-w-md rounded-md p-5">
        <div className="mb-5">
          <div className="flex items-center gap-2">
            <img src="/lazycloud.png" alt="" className="size-8" />
            <span className="text-xl font-bold text-brand">LazyCloud</span>
          </div>
          <h1 className="mt-3 text-xl font-semibold">Approve CLI sign-in</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Confirm the code shown in your terminal to connect the CLI to a workspace.
          </p>
        </div>
        {userCode.length === 9 ? <DeviceCodePanel userCode={userCode} /> : <CodeEntryForm />}
      </section>
    </main>
  );
}

function CodeEntryForm({ error }: { error?: string }) {
  const navigate = useNavigate();
  const [value, setValue] = useState("");

  return (
    <form
      className="space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        const normalized = normalizeUserCode(value);
        if (normalized.length === 9) {
          void navigate({ to: "/activate", search: { code: normalized } });
        }
      }}
    >
      {error ? (
        <div className="rounded border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}
      <label className="block text-xs font-medium text-muted-foreground">
        Code
        <input
          value={value}
          onChange={(event) => setValue(event.target.value.toUpperCase())}
          placeholder="XXXX-XXXX"
          autoFocus
          className="mono mt-1 h-9 w-full rounded-md border border-input bg-muted px-3 text-sm text-foreground outline-none focus:border-ring"
        />
      </label>
      <Button type="submit" className="w-full" disabled={normalizeUserCode(value).length !== 9}>
        Continue
      </Button>
    </form>
  );
}

function DeviceCodePanel({ userCode }: { userCode: string }) {
  const deviceCode = useQuery(deviceCodeQueryOptions(userCode));

  if (deviceCode.isPending) {
    return (
      <div className="flex h-24 items-center justify-center">
        <Loader2 className="size-5 animate-spin text-brand" />
      </div>
    );
  }
  if (deviceCode.isError) {
    return (
      <CodeEntryForm
        error={
          deviceCode.error instanceof ApiError && deviceCode.error.status === 404
            ? `Code ${userCode} was not found. It may have expired — run the login command again.`
            : "The code could not be checked. Retry or run the login command again."
        }
      />
    );
  }
  return <DeviceCodeDecision userCode={userCode} deviceCode={deviceCode.data} />;
}

function DeviceCodeDecision({ userCode, deviceCode }: { userCode: string; deviceCode: DeviceCode }) {
  const { workspaces } = useSession();
  const activeWorkspaces = workspaces.filter((item) => item.status === "active");
  const [workspace, setWorkspace] = useState(activeWorkspaces[0]?.name ?? "");
  const approve = useMutation(approveDeviceCodeMutationOptions());
  const deny = useMutation(denyDeviceCodeMutationOptions());

  if (approve.isSuccess) {
    return (
      <Outcome tone="positive" title="CLI connected">
        The CLI now has access to the <span className="font-medium">{workspace}</span> workspace.
        You can return to your terminal.
      </Outcome>
    );
  }
  if (deny.isSuccess) {
    return (
      <Outcome tone="muted" title="Sign-in denied">
        The request was denied. The CLI will stop waiting shortly.
      </Outcome>
    );
  }
  if (deviceCode.status !== "pending") {
    return (
      <CodeEntryForm
        error={
          deviceCode.status === "expired"
            ? `Code ${userCode} has expired. Run the login command again for a fresh code.`
            : `Code ${userCode} was already ${deviceCode.status}.`
        }
      />
    );
  }

  const failure = approve.error ?? deny.error;
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-border bg-muted/40 p-3">
        <div className="micro-label">Code</div>
        <div className="mono mt-1 text-lg">{deviceCode.user_code}</div>
        <div className="mt-2 text-sm text-muted-foreground">
          Requested by <span className="mono text-foreground/90">{deviceCode.client_name}</span>{" "}
          {relativeTime(deviceCode.created_at)}
        </div>
      </div>

      {failure ? (
        <div className="rounded border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
          {failure instanceof ApiError && failure.status === 401
            ? "Your token cannot authorize this workspace."
            : failure.message}
        </div>
      ) : null}

      <label className="block text-xs font-medium text-muted-foreground">
        Workspace to authorize
        <Select value={workspace} onValueChange={setWorkspace}>
          <SelectTrigger className="mt-1 w-full">
            <SelectValue placeholder="Select a workspace" />
          </SelectTrigger>
          <SelectContent>
            {activeWorkspaces.map((item) => (
              <SelectItem key={item.id} value={item.name}>
                {item.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </label>

      <div className="flex gap-2">
        <Button
          className="flex-1"
          disabled={!workspace || approve.isPending || deny.isPending}
          onClick={() => approve.mutate({ userCode, workspace })}
        >
          {approve.isPending ? <Loader2 className="size-4 animate-spin" /> : "Approve"}
        </Button>
        <Button
          variant="outline"
          className="flex-1"
          disabled={approve.isPending || deny.isPending}
          onClick={() => deny.mutate({ userCode })}
        >
          {deny.isPending ? <Loader2 className="size-4 animate-spin" /> : "Deny"}
        </Button>
      </div>
    </div>
  );
}

function Outcome({
  tone,
  title,
  children,
}: {
  tone: "positive" | "muted";
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span
          className={`size-2 rounded-full ${tone === "positive" ? "bg-positive" : "bg-muted-foreground"}`}
          aria-hidden="true"
        />
        <span className="text-sm font-semibold">{title}</span>
      </div>
      <p className="text-sm text-muted-foreground">{children}</p>
      <Button variant="outline" size="sm" asChild>
        <Link to="/">Go to dashboard</Link>
      </Button>
    </div>
  );
}
