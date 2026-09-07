import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";

import { PreShellScreen } from "@/components/shared/PreShellScreen";
import { Button } from "@/components/ui/button";
import { ApiError, setAuthToken } from "@/lib/api/client";
import { completeSignInMutationOptions, githubSignInHref } from "@/lib/queries/auth";

export const Route = createFileRoute("/callback")({
  component: SignInCallbackPage,
});

const SIGNED_OUT_PATHS = new Set(["", "/", "/signin", "/callback"]);

function signedInDestination(returnTo: string): string {
  const normalized = returnTo.split(/[?#]/)[0].replace(/\/+$/, "");
  return SIGNED_OUT_PATHS.has(normalized) ? "/dashboard" : returnTo;
}

function SignInCallbackPage() {
  const navigate = useNavigate();
  // Read before clearing the fragment. Neither the exchange code nor an invitation
  // path belongs in browser storage or the callback's query string.
  const [{ code, returnTo }] = useState(() => {
    const fragment = new URLSearchParams(
      typeof window === "undefined" ? "" : window.location.hash.replace(/^#/, ""),
    );
    return {
      code: fragment.get("code") ?? "",
      returnTo: signedInDestination(fragment.get("return_to") ?? ""),
    };
  });
  // StrictMode runs effects twice; redemption consumes the code on its first use.
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
    if (redeemed.current) return;
    redeemed.current = true;
    window.history.replaceState(window.history.state, "", window.location.pathname);
    if (code) mutate({ code });
  }, [code, mutate]);

  const failure = complete.isError
    ? complete.error instanceof ApiError &&
      complete.error.status >= 400 &&
      complete.error.status < 500 &&
      complete.error.status !== 408 &&
      complete.error.status !== 429
      ? "This sign-in link could not be verified. Start sign-in again."
      : "Sign-in is temporarily unavailable. Try again."
    : // The prerender has no URL fragment and must not bake in a missing-code error.
      typeof window !== "undefined" && !code
      ? "This sign-in link is missing its code."
      : null;

  if (failure) {
    return (
      <PreShellScreen role="alert">
        <h1 className="text-xl font-semibold">Sign-in did not complete</h1>
        <p className="mt-1 text-sm text-muted-foreground">{failure}</p>
        <Button asChild className="mt-4 w-full">
          <a href={githubSignInHref(returnTo)}>Start sign-in again</a>
        </Button>
      </PreShellScreen>
    );
  }

  return (
    <PreShellScreen>
      <p
        role="status"
        className="flex items-center justify-center gap-2 text-sm text-muted-foreground"
      >
        <Loader2 className="size-4 animate-spin text-brand" aria-hidden="true" />
        Completing sign-in
      </p>
    </PreShellScreen>
  );
}
