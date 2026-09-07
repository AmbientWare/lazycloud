import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { setAuthToken } from "@/lib/api/client";
import { completeSignInMutationOptions, githubSignInHref } from "@/lib/queries/auth";

export const Route = createFileRoute("/callback")({
  component: SignInCallbackPage,
});

// Pages that mean "not signed in". Returning to one of them after signing in
// strands the person on a logged-out page holding a live session, so the product
// is the destination instead — whatever the return path said.
const SIGNED_OUT_PATHS = new Set(["", "/", "/signin", "/callback"]);

function signedInDestination(returnTo: string): string {
  const normalized = returnTo.split("?")[0].replace(/\/+$/, "");
  return SIGNED_OUT_PATHS.has(normalized) ? "/dashboard" : returnTo;
}

/**
 * Where GitHub's callback lands the browser, carrying a single-use code.
 *
 * The code arrives in the fragment rather than the query, so it never reaches a
 * server log or a `Referer` header. It is read once and dropped from the URL, and
 * the session it buys is minted by the exchange rather than carried here.
 */
function SignInCallbackPage() {
  const navigate = useNavigate();
  // Read once, before the effect clears the fragment. A lazy initializer only reads,
  // so re-running it under StrictMode's remount yields the same code. The build
  // prerenders this route with no `window`, where there is no fragment to read and
  // the spinner below is the right thing to bake into the static shell.
  const [code] = useState(() =>
    typeof window === "undefined"
      ? ""
      : (new URLSearchParams(window.location.hash.replace(/^#/, "")).get("code") ?? ""),
  );
  // StrictMode mounts twice and the code is spent on first use. Without this guard
  // the second attempt fails and the person sees a broken sign-in.
  const redeemed = useRef(false);
  const complete = useMutation({
    ...completeSignInMutationOptions(),
    onSuccess: (session) => {
      setAuthToken(session.token);
      // `replace`, so Back cannot return to a callback URL whose code is spent.
      void navigate({ to: signedInDestination(session.return_to), replace: true });
    },
  });

  const { mutate } = complete;
  useEffect(() => {
    if (redeemed.current || !code) return;
    redeemed.current = true;
    // Drop the code from the address bar so it does not sit in history.
    window.history.replaceState(null, "", window.location.pathname);
    mutate({ code });
  }, [code, mutate]);

  const failure = complete.isError
    ? "That sign-in link has expired or was already used."
    : // Only the browser can know the fragment was empty; during the prerender it
      // always is, and reporting that as a failure would bake it into the page.
      typeof window !== "undefined" && !code
      ? "This sign-in link is missing its code."
      : null;

  if (failure) {
    return (
      <PreShellScreen>
        <h1 className="text-xl font-semibold">Sign-in did not complete</h1>
        <p className="mt-1 text-sm text-muted-foreground">{failure}</p>
        <Button asChild className="mt-4 w-full">
          <a href={githubSignInHref("/dashboard")}>Try again</a>
        </Button>
      </PreShellScreen>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-background">
      <Loader2 className="size-6 animate-spin text-brand" />
    </main>
  );
}
