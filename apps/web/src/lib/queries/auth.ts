import { mutationOptions, queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest } from "@/lib/api/client";
import { currentSessionSchema, deviceCodeSchema, sessionSchema } from "@/lib/api/schemas";

export function signInMutationOptions() {
  return mutationOptions({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      apiRequest("/api/v1/sessions", sessionSchema, {
        method: "POST",
        body: JSON.stringify({ username, password }),
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

export function changePasswordMutationOptions() {
  return mutationOptions({
    mutationFn: ({
      userId,
      currentPassword,
      newPassword,
    }: {
      userId: string;
      currentPassword: string;
      newPassword: string;
    }) =>
      apiRequest(`/api/v1/users/${encodeURIComponent(userId)}/password`, z.null(), {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      }),
  });
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
