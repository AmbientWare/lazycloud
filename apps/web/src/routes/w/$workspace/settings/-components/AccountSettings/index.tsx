import { Check, Loader2 } from "lucide-react";

import { CopyId } from "@/components/shared/CopyId";
import { Panel } from "@/components/shared/Panel";
import { StatusChip } from "@/components/shared/StatusChip";
import { useSession } from "@/components/shared/AuthGate/session";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { relativeTime } from "@/lib/format";

import { usePasswordChangeController } from "./controller";

/** Who you are signed in as. Identical in every workspace, because it is not about one. */
export function AccountSettings() {
  const { user } = useSession();

  return (
    <div className="grid gap-4 lg:grid-cols-5">
      <Panel title="Account" className="h-fit lg:col-span-3">
        <dl className="grid grid-cols-2 gap-x-5 gap-y-4 p-4 sm:grid-cols-4">
          <AccountFact label="Username" value={user.username} />
          <div className="min-w-0">
            <dt className="micro-label mb-1.5">User ID</dt>
            <dd>
              <CopyId value={user.id} className="max-w-full px-0" />
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="micro-label mb-1.5">Status</dt>
            <dd>
              <StatusChip status={user.status} />
            </dd>
          </div>
          <AccountFact label="Created" value={relativeTime(user.created_at)} />
        </dl>
      </Panel>
      <PasswordPanel userId={user.id} />
    </div>
  );
}

function PasswordPanel({ userId }: { userId: string }) {
  const password = usePasswordChangeController({ userId });
  // The blocker is guidance while typing, not an error; it only becomes a complaint
  // once there is something to complain about.
  const notice = password.error || (password.isSaved ? "" : password.blocker);

  return (
    <Panel
      title="Password"
      description="Changing it signs out your other sessions"
      className="h-fit lg:col-span-2"
    >
      <form
        className="space-y-3 p-4"
        onSubmit={(event) => {
          event.preventDefault();
          password.submit();
        }}
      >
        <PasswordField
          label="Current password"
          autoComplete="current-password"
          value={password.currentPassword}
          onChange={password.setCurrentPassword}
        />
        <PasswordField
          label="New password"
          autoComplete="new-password"
          value={password.newPassword}
          onChange={password.setNewPassword}
        />
        <PasswordField
          label="Confirm new password"
          autoComplete="new-password"
          value={password.confirmPassword}
          onChange={password.setConfirmPassword}
        />
        {password.error ? (
          <p className="text-xs text-destructive" role="alert">
            {password.error}
          </p>
        ) : password.isSaved ? (
          <p className="text-xs text-positive">Password changed.</p>
        ) : notice ? (
          <p className="text-xs text-muted-foreground">{notice}</p>
        ) : null}
        <Button type="submit" size="sm" disabled={!password.canSubmit}>
          {password.isSaving ? <Loader2 className="animate-spin" /> : <Check />}
          Change password
        </Button>
      </form>
    </Panel>
  );
}

function PasswordField({
  label,
  autoComplete,
  value,
  onChange,
}: {
  label: string;
  autoComplete: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="micro-label mb-1.5 block">{label}</span>
      <Input
        type="password"
        autoComplete={autoComplete}
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function AccountFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="micro-label mb-1.5">{label}</dt>
      <dd className="truncate text-sm font-medium">{value}</dd>
    </div>
  );
}
