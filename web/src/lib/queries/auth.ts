import { mutationOptions, queryOptions } from "@tanstack/react-query";
import type { z } from "zod";

import { api, ok } from "@/lib/api/client";
import type { CurrentSession, DeviceCode, sessionSchema } from "@/lib/api/schemas";
import { viewUser, viewWorkspace } from "@/lib/api/views";
import { rememberWorkspaces } from "@/lib/api/workspaces";

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

/**
 * Finish the sign-in the callback page lands on: the cookie is already set, so
 * this confirms it opened a session and hands back where the browser was going.
 */
export function completeSignInMutationOptions() {
  return mutationOptions({
    mutationFn: async ({ code }: { code: string }): Promise<z.infer<typeof sessionSchema>> => {
      const me = await ok(api.GET("/v1/me"));
      rememberWorkspaces(me.workspaces);
      return {
        token: SESSION_MARKER,
        expires_at: new Date(Date.now() + 12 * 3_600_000).toISOString(),
        user: viewUser(me.user),
        return_to: decodeURIComponent(code),
      };
    },
  });
}

export function currentSessionQueryOptions() {
  return queryOptions({
    queryKey: ["auth", "session"],
    queryFn: async (): Promise<CurrentSession> => {
      const me = await ok(api.GET("/v1/me"));
      rememberWorkspaces(me.workspaces);
      return { user: viewUser(me.user), workspaces: me.workspaces.map(viewWorkspace) };
    },
    retry: false,
  });
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
    queryFn: (): Promise<DeviceCode> =>
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
    mutationFn: ({ userCode }: { userCode: string }): Promise<DeviceCode> =>
      ok(api.POST("/v1/device-codes/{user_code}/approve", devicePath(userCode))),
  });
}

export function denyDeviceCodeMutationOptions() {
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }): Promise<DeviceCode> =>
      ok(api.POST("/v1/device-codes/{user_code}/deny", devicePath(userCode))),
  });
}
