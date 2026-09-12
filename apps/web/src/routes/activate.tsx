import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { AuthGate } from "@/components/shared/AuthGate";
import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSession } from "@/components/shared/AuthGate/session";
import { ApiError } from "@/lib/api/client";
import type { DeviceCode } from "@/lib/api/schemas";
import {
  approveDeviceCodeMutationOptions,
  denyDeviceCodeMutationOptions,
  deviceCodeQueryOptions,
} from "@/lib/queries/auth";

export const Route = createFileRoute("/activate")({
  validateSearch: (search: Record<string, unknown>): { code: string } => ({
    code: typeof search.code === "string" ? search.code : "",
  }),
  component: () => (
    <AuthGate>
      <ActivatePage />
    </AuthGate>
  ),
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
    <PreShellScreen>
      <div className="mb-5">
        <div className="flex items-center gap-2">
          <img src="/lazycloud.png" alt="" className="size-8" />
          <span className="text-xl font-bold text-brand">LazyCloud</span>
        </div>
        <h1 className="mt-3 text-xl font-semibold">Approve CLI sign-in</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Match the code in your terminal to sign the CLI in.
        </p>
      </div>
      {userCode.length === 9 ? <DeviceCodePanel userCode={userCode} /> : <CodeEntryForm />}
    </PreShellScreen>
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
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}
      <label className="block text-xs font-medium text-muted-foreground">
        Code
        <Input
          value={value}
          onChange={(event) => setValue(event.target.value.toUpperCase())}
          placeholder="XXXX-XXXX"
          autoFocus
          className="mono mt-1"
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
            ? `Code ${userCode} was not found. It may have expired. Run the login command again.`
            : "Could not check the code. Retry or run the login command again."
        }
      />
    );
  }
  return <DeviceCodeDecision key={userCode} userCode={userCode} deviceCode={deviceCode.data} />;
}

function DeviceCodeDecision({
  userCode,
  deviceCode,
}: {
  userCode: string;
  deviceCode: DeviceCode;
}) {
  const { user } = useSession();
  const queryClient = useQueryClient();
  const mutationKey = ["auth", "device-code", userCode, "decision"];
  const approve = useMutation({ ...approveDeviceCodeMutationOptions(), mutationKey });
  const deny = useMutation({ ...denyDeviceCodeMutationOptions(), mutationKey });
  const decide = (action: "approve" | "deny") => {
    if (queryClient.isMutating({ mutationKey, exact: true })) return;
    (action === "approve" ? approve : deny).mutate({ userCode });
  };

  if (approve.isSuccess) {
    return (
      <Outcome tone="positive" title="CLI connected">
        The CLI is signed in as <span className="font-medium">{user.display_name}</span> and can
        access your workspaces. Return to your terminal.
      </Outcome>
    );
  }
  if (deny.isSuccess) {
    return (
      <Outcome tone="muted" title="Sign-in denied">
        The request was denied. The CLI will stop waiting.
      </Outcome>
    );
  }
  if (deviceCode.status !== "pending") {
    return (
      <CodeEntryForm
        error={
          deviceCode.status === "expired"
            ? `Code ${userCode} expired. Run the login command again.`
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
          <LiveRelativeTime value={deviceCode.created_at} />
        </div>
      </div>

      {failure ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive"
        >
          {failure instanceof ApiError && failure.status === 403
            ? "Sign in with an account to approve the CLI. Workspace tokens cannot approve it."
            : failure.message}
        </div>
      ) : null}

      <p className="text-sm text-muted-foreground">
        This signs the CLI in as <span className="font-medium">{user.display_name}</span> with
        access to your workspaces. The CLI chooses its active workspace.
      </p>

      <div className="flex gap-2">
        <Button
          className="flex-1"
          disabled={approve.isPending || deny.isPending}
          onClick={() => decide("approve")}
        >
          {approve.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
          Approve
        </Button>
        <Button
          variant="outline"
          className="flex-1"
          disabled={approve.isPending || deny.isPending}
          onClick={() => decide("deny")}
        >
          {deny.isPending ? <Loader2 className="size-4 animate-spin" /> : null}
          Deny
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
        <Link to="/dashboard">Go to dashboard</Link>
      </Button>
    </div>
  );
}
