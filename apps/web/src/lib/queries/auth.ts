import { mutationOptions, queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/client";
import { deviceCodeSchema } from "@/lib/api/schemas";

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
  return mutationOptions({
    mutationFn: ({ userCode, workspace }: { userCode: string; workspace: string }) =>
      apiRequest(`/api/v1/device-codes/${encodeURIComponent(userCode)}/approve`, deviceCodeSchema, {
        method: "POST",
        body: JSON.stringify({ workspace }),
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
