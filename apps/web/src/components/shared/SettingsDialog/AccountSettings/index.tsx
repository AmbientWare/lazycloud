import { useSession } from "@/components/shared/AuthGate/session";

/** The customer-facing profile for the signed-in account. */
export function AccountSettings() {
  const { user } = useSession();

  return (
    <section className="panel flex min-w-0 items-center gap-3 rounded-md px-4 py-4">
      <div className="flex min-w-0 items-center gap-3">
        {user.avatar_url ? (
          <img src={user.avatar_url} alt="" className="size-10 shrink-0 rounded-full" />
        ) : null}
        <div className="min-w-0">
          <h2 className="truncate text-base font-semibold tracking-tight">{user.display_name}</h2>
          {user.email ? (
            <p className="mt-0.5 truncate text-sm text-muted-foreground">{user.email}</p>
          ) : null}
        </div>
      </div>
    </section>
  );
}
