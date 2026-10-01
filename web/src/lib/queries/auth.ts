import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";

/** The marker the page keeps once it holds a session; the cookie itself is HttpOnly. */
export const SESSION_MARKER = "session";

/**
 * Where the browser goes to sign in.
 *
 * A URL rather than a request: the server sets the sign-in cookie and redirects to
 * GitHub, so this has to be a document navigation. The server's callback sets the
 * HttpOnly session cookie and returns the browser to `/callback`, whose fragment
 * carries the destination; a browser that is already signed in goes straight there.
 */
export function githubSignInHref(returnTo: string): string {
  const callback = `/callback#code=${encodeURIComponent(returnTo)}`;
  return `/auth/github/start?return_to=${encodeURIComponent(callback)}`;
}

// Pages that mean "not signed in". Returning to one of them after signing in
// strands the person on a logged-out page holding a live session, so the product
// is the destination instead.
const SIGNED_OUT_PATHS = new Set(["", "/", "/signin", "/callback"]);

/**
 * Where the browser goes after signing in, from the `code` the `/callback` fragment
 * carries. The fragment parameter is already decoded once and is used as it is.
 *
 * Only a path on this origin is followed. Anything else, including `//host`, and
 * `/\host` or an embedded tab that a browser also reads as another origin, lands on
 * the dashboard.
 */
export function signedInDestination(returnTo: string): string {
  if (!returnTo.startsWith("/") || returnTo.startsWith("//")) return "/dashboard";
  const origin = "https://dashboard.invalid";
  let url: URL;
  try {
    url = new URL(returnTo, origin);
  } catch {
    return "/dashboard";
  }
  if (url.origin !== origin) return "/dashboard";
  return SIGNED_OUT_PATHS.has(url.pathname.replace(/\/+$/, "")) ? "/dashboard" : returnTo;
}

/**
 * Finish the sign-in the callback page lands on: the cookie is already set, so
 * this confirms it opened a session.
 */
export function completeSignInMutationOptions() {
  return mutationOptions({
    mutationFn: () => readSession(),
  });
}

export function currentSessionQueryOptions() {
  return queryOptions({
    queryKey: ["auth", "session"],
    queryFn: () => readSession(),
    retry: false,
  });
}

function readSession(): Promise<Schemas["Me"]> {
  return ok(api.GET("/v1/me"));
}

/** End the session this browser holds, so signing out stops the credential working. */
export async function signOut(): Promise<null> {
  await ok(api.DELETE("/v1/sessions/current"));
  return null;
}

const devicePath = (userCode: string) => ({ params: { path: { user_code: userCode } } });

export function deviceCodeQueryOptions(userCode: string) {
  return queryOptions({
    queryKey: ["auth", "device-code", userCode],
    queryFn: (): Promise<Schemas["DeviceCode"]> =>
      ok(api.GET("/v1/device-codes/{user_code}", devicePath(userCode))),
    enabled: userCode.length > 0,
    staleTime: 0,
    retry: false,
  });
}

export function approveDeviceCodeMutationOptions() {
  // No workspace: approving grants the CLI the signed-in account's own reach, and
  // the CLI chooses which of that account's workspaces is active.
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }): Promise<Schemas["DeviceCode"]> =>
      ok(api.POST("/v1/device-codes/{user_code}/approve", devicePath(userCode))),
  });
}

export function denyDeviceCodeMutationOptions() {
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }): Promise<Schemas["DeviceCode"]> =>
      ok(api.POST("/v1/device-codes/{user_code}/deny", devicePath(userCode))),
  });
}
