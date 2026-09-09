import { useSession } from "@/components/shared/AuthGate/session";

export function AccountSettings() {
  const { user } = useSession();

  return (
    <section className="flex min-w-0 items-center gap-3 border-b border-border pb-3">
      <div className="flex min-w-0 items-center gap-3">
        {user.avatar_url ? (
          <img src={user.avatar_url} alt="" className="size-8 shrink-0 rounded-full" />
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
