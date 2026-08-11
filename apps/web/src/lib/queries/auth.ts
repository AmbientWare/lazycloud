import { mutationOptions, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import { currentSessionSchema, deviceCodeSchema, sessionSchema } from "@/lib/api/schemas";

/**
 * Where the browser goes to sign in.
 *
 * A URL rather than a request: the server sets the sign-in cookie and redirects to
 * GitHub, so this has to be a document navigation. `returnTo` is handed to the
 * server and kept against the state secret, never carried through GitHub.
 */
export function githubSignInHref(returnTo: string): string {
  return `/auth/github/start?return_to=${encodeURIComponent(returnTo)}`;
}

/** Trade the single-use code from the callback for the session credential. */
export function completeSignInMutationOptions() {
  return mutationOptions({
    mutationFn: ({ code }: { code: string }) =>
      apiRequest("/api/v1/sessions", sessionSchema, {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
  });
}

export function currentSessionQueryOptions() {
  return queryOptions({
    queryKey: ["auth", "session"],
    queryFn: () => apiRequest("/api/v1/sessions/current", currentSessionSchema),
    retry: false,
  });
}

/** End the session this browser holds, so signing out stops the credential working. */
export function signOut(): Promise<null> {
  return apiRequest("/api/v1/sessions/current", z.null(), { method: "DELETE" });
}

export function deviceCodeQueryOptions(userCode: string) {
  return queryOptions({
    queryKey: ["auth", "device-code", userCode],
    queryFn: () =>
      apiRequest(`/api/v1/device-codes/${encodeURIComponent(userCode)}`, deviceCodeSchema),
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
      apiRequest(`/api/v1/device-codes/${encodeURIComponent(userCode)}/approve`, deviceCodeSchema, {
        method: "POST",
      }),
  });
}

export function denyDeviceCodeMutationOptions() {
  return mutationOptions({
    mutationFn: ({ userCode }: { userCode: string }) =>
      apiRequest(`/api/v1/device-codes/${encodeURIComponent(userCode)}/deny`, deviceCodeSchema, {
        method: "POST",
      }),
  });
}
