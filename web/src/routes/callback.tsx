import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { setStoredAuthToken } from "@/lib/auth";
import {
  completeSignInMutationOptions,
  currentSessionQueryOptions,
  githubSignInHref,
  SESSION_MARKER,
  signedInDestination,
} from "@/lib/queries/auth";

export const Route = createFileRoute("/callback")({
  component: SignInCallbackPage,
});

/**
 * Where the server's GitHub callback returns the browser once it has set the
 * HttpOnly session cookie.
 *
 * The fragment's `code` names where the browser was going. It stays out of server
 * logs and `Referer` headers, and it is read once and dropped from the URL. A read
 * of `/v1/me` confirms the cookie opened a session before the page keeps its marker.
 */
function SignInCallbackPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Read once, before the effect clears the fragment. A lazy initializer only reads,
  // so re-running it under StrictMode's remount yields the same value. The build
  // prerenders this route with no `window`, where there is no fragment to read and
  // the spinner below is the right thing to bake into the static shell.
  const [returnTo] = useState(() =>
    typeof window === "undefined"
      ? ""
      : (new URLSearchParams(window.location.hash.replace(/^#/, "")).get("code") ?? ""),
  );
  // StrictMode mounts twice; one confirmation is enough.
  const confirmed = useRef(false);
  const complete = useMutation({
    ...completeSignInMutationOptions(),
    onSuccess: (session) => {
      // Nothing cached for a previous account survives into this one, and the read
      // that confirmed the session is the shell's first answer.
      queryClient.clear();
      queryClient.setQueryData(currentSessionQueryOptions().queryKey, session);
      setStoredAuthToken(SESSION_MARKER);
      // `replace`, so Back does not return to the callback URL.
      void navigate({ to: signedInDestination(returnTo), replace: true });
    },
  });

  const { mutate } = complete;
  useEffect(() => {
    if (confirmed.current || !returnTo) return;
    confirmed.current = true;
    // Drop the fragment from the address bar so it does not sit in history.
    window.history.replaceState(null, "", window.location.pathname);
    mutate();
  }, [returnTo, mutate]);

  const failure = complete.isError
    ? complete.error instanceof ApiError && complete.error.status === 401
      ? "That sign-in link has expired or was already used."
      : "Could not complete sign-in. Please start again."
    : // Only the browser can know the fragment was empty; during the prerender it
      // always is, and reporting that as a failure would bake it into the page.
      typeof window !== "undefined" && !returnTo
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
