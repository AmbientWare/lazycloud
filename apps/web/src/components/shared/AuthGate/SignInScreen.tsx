import { useRouterState } from "@tanstack/react-router";

import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { githubSignInHref } from "@/lib/queries/auth";

/** The one way in. Rendered by the auth gate in place, and by `/signin` after a failure. */
export function SignInScreen({ error }: { error?: string }) {
  // From the router rather than from `window`, because this markup is prerendered.
  // Reading `window` there yields a fallback that gets baked into the static HTML,
  // and a click landing before hydration then returns the person to that fallback
  // instead of where they were going.
  const location = useRouterState({ select: (state) => state.location });
  const returnTo = `${location.pathname}${location.searchStr}${location.hash ? `#${location.hash}` : ""}`;
  // Arriving on the device-approval URL while signed out: the return path carries
  // the code through GitHub and back, so say why this is showing first.
  const approvingDevice = location.pathname.replace(/\/+$/, "") === "/activate";

  return (
    <PreShellScreen>
      <div className="mb-5">
        <div className="flex items-center gap-2">
          <img src="/lazycloud.png" alt="" className="size-8" />
          <span className="text-xl font-bold text-brand">LazyCloud</span>
        </div>
        <h1 className="mt-3 text-xl font-semibold">Sign in</h1>
        <p className="mt-1 text-sm text-muted-foreground">Use your GitHub account to continue.</p>
      </div>

      {approvingDevice ? (
        <div className="mb-3 rounded border border-border bg-muted/40 p-2 text-sm text-muted-foreground">
          A CLI is waiting. Sign in to approve it.
        </div>
      ) : null}

      {error ? (
        <div
          role="alert"
          className="mb-3 rounded border border-destructive/40 bg-destructive/10 p-2 text-sm text-destructive"
        >
          {error}
        </div>
      ) : null}

      {/* A real anchor rather than a scripted click: leaving for GitHub is a document
          navigation, so middle-click and right-click behave the way they look. */}
      <Button asChild className="w-full">
        <a href={githubSignInHref(returnTo)}>Continue with GitHub</a>
      </Button>
    </PreShellScreen>
  );
}
