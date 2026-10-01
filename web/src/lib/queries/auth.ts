import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { api, isApiError, ok } from "@/lib/api/client";

/**
 * Where the browser goes to sign in.
 *
 * A URL rather than a request: the server sets the sign-in cookie and redirects to
 * GitHub, so this has to be a document navigation. `returnTo` is handed to the
 * server and kept against the state secret, never carried through GitHub. The
 * callback sets the session cookie and redirects straight to it.
 */
export function githubSignInHref(returnTo: string): string {
  return `/auth/github/start?return_to=${encodeURIComponent(returnTo)}`;
}

export const meQueryKey = ["auth", "me"] as const;

/**
 * Who the session cookie belongs to and the workspaces they are a member of, or
 * null when the browser holds no live session. The cookie is HttpOnly, so this
 * request is the only way the page learns whether it is signed in.
 */
export function meQueryOptions() {
  return queryOptions({
    queryKey: meQueryKey,
    queryFn: async () => {
      try {
        return await ok(api.GET("/v1/me"));
      } catch (error) {
        if (isApiError(error, 401)) return null;
        throw error;
      }
    },
    retry: false,
  });
}

/** End the session this browser holds, and no other. */
export function signOut(): Promise<void> {
  return ok(api.DELETE("/v1/sessions/current"));
}

export function deviceCodeQueryOptions(userCode: string) {
  return queryOptions({
    queryKey: ["auth", "device-code", userCode],
    queryFn: () =>
      ok(api.GET("/v1/device-codes/{user_code}", { params: { path: { user_code: userCode } } })),
    enabled: userCode.length > 0,
    staleTime: 0,
    retry: false,
  });
}

export function approveDeviceCodeMutationOptions() {
  // No workspace: approving grants the CLI the signed-in account's own reach, and
  // the CLI chooses which of that account's workspaces is active.
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }) =>
      ok(
        api.POST("/v1/device-codes/{user_code}/approve", {
          params: { path: { user_code: userCode } },
        }),
      ),
  });
}

export function denyDeviceCodeMutationOptions() {
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }) =>
      ok(
        api.POST("/v1/device-codes/{user_code}/deny", {
          params: { path: { user_code: userCode } },
        }),
      ),
  });
}
