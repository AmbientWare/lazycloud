import { CopyId } from "@/components/shared/CopyId";
import { StatusChip } from "@/components/shared/StatusChip";
import { useSession } from "@/components/shared/AuthGate/session";
import { relativeTime } from "@/lib/format";

/** Who you are signed in as. Identical in every workspace, because it is not about one. */
export function AccountSettings() {
  const { user } = useSession();

  return (
    <section className="panel flex flex-wrap items-center gap-x-6 gap-y-3 rounded-md px-4 py-3">
      {/* The person is the largest thing on a page about them. A label-value grid
          would have set their name in the same size as the word "Name". */}
      <div className="flex min-w-0 items-center gap-2.5">
        {user.avatar_url ? (
          <img src={user.avatar_url} alt="" className="size-8 shrink-0 rounded-full" />
        ) : null}
        <h2 className="truncate text-xl font-semibold tracking-tight">{user.display_name}</h2>
        <StatusChip status={user.status} />
      </div>
      <dl className="flex min-w-0 flex-wrap items-center gap-x-6 gap-y-1 text-xs">
        {user.github_login ? (
          <div className="flex items-center gap-2">
            <dt className="micro-label">GitHub</dt>
            <dd className="mono text-foreground/90">{user.github_login}</dd>
          </div>
        ) : null}
        {user.email ? (
          <div className="flex min-w-0 items-center gap-2">
            <dt className="micro-label">Email</dt>
            <dd className="truncate text-foreground/90">{user.email}</dd>
          </div>
        ) : null}
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
    </section>
  );
}
