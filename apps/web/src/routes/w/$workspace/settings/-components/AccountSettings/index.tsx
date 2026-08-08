import { useState } from "react";
import { Check, KeyRound, Loader2 } from "lucide-react";

import { CopyId } from "@/components/shared/CopyId";
import { StatusChip } from "@/components/shared/StatusChip";
import { useSession } from "@/components/shared/AuthGate/session";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { relativeTime } from "@/lib/format";

import { usePasswordChangeController } from "./controller";

/** Who you are signed in as. Identical in every workspace, because it is not about one. */
export function AccountSettings() {
  const { user } = useSession();
  const [resetOpen, setResetOpen] = useState(false);

  return (
    <section className="panel flex flex-wrap items-center gap-x-6 gap-y-3 rounded-md px-4 py-3">
      {/* The person is the largest thing on a page about them. A label-value grid
          would have set their name in the same size as the word "Username". */}
      <div className="flex min-w-0 items-center gap-2.5">
        <h2 className="truncate text-xl font-semibold tracking-tight">{user.username}</h2>
        <StatusChip status={user.status} />
      </div>
      <dl className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-1 text-xs">
        <div className="flex min-w-0 items-center gap-2">
          <dt className="micro-label">User ID</dt>
          <dd className="min-w-0">
            <CopyId value={user.id} className="max-w-full px-0" />
          </dd>
        </div>
        <div className="flex items-center gap-2">
          <dt className="micro-label">Created</dt>
          <dd className="text-foreground/90">{relativeTime(user.created_at)}</dd>
        </div>
      </dl>
      <Button
        variant="outline"
        size="sm"
        className="ml-auto shrink-0"
        onClick={() => setResetOpen(true)}
      >
        <KeyRound />
        Reset password
      </Button>
      <ResetPasswordDialog userId={user.id} open={resetOpen} onOpenChange={setResetOpen} />
    </section>
  );
}

function ResetPasswordDialog({
  userId,
  open,
  onOpenChange,
}: {
  userId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open, so the fields hold no password once it closes. */}
      {open ? <ResetPasswordForm userId={userId} onDone={() => onOpenChange(false)} /> : null}
    </Dialog>
  );
}

function ResetPasswordForm({ userId, onDone }: { userId: string; onDone: () => void }) {
  const password = usePasswordChangeController({ userId, onChanged: onDone });

  return (
    <DialogContent className="max-w-md sm:max-w-md">
      <DialogHeader>
        <DialogTitle className="flex items-center gap-2 text-base">
          <KeyRound className="size-4 text-brand" />
          Reset password
        </DialogTitle>
        <DialogDescription>
          Your current password confirms it is you. Your other sessions are signed out.
        </DialogDescription>
      </DialogHeader>
      <form
        className="space-y-3"
        onSubmit={(event) => {
          event.preventDefault();
          password.submit();
        }}
      >
        <PasswordField
          label="Current password"
          autoComplete="current-password"
          autoFocus
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
        <PasswordNotice error={password.error} guidance={password.guidance} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="ghost" size="sm" onClick={onDone}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={!password.canSubmit}>
            {password.isSaving ? <Loader2 className="animate-spin" /> : <Check />}
            Reset password
          </Button>
        </div>
      </form>
    </DialogContent>
  );
}

/** One line, one job: what went wrong, or what is still missing. */
function PasswordNotice({ error, guidance }: { error: string; guidance: string }) {
  if (error) {
    return (
      <p className="text-xs text-destructive" role="alert">
        {error}
      </p>
    );
  }
  if (guidance) {
    return <p className="text-xs text-muted-foreground">{guidance}</p>;
  }
  return null;
}

function PasswordField({
  label,
  autoComplete,
  autoFocus,
  value,
  onChange,
}: {
  label: string;
  autoComplete: string;
  autoFocus?: boolean;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="micro-label mb-1.5 block">{label}</span>
      <Input
        type="password"
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
