import { useSyncExternalStore } from "react";

import { getStoredAuthToken, subscribeStoredAuthToken } from "@/lib/auth";

const clientToken = (): string | null | undefined => getStoredAuthToken();
const serverToken = (): string | null | undefined => undefined;

export function useAuthToken(): string | null | undefined {
  return useSyncExternalStore(subscribeStoredAuthToken, clientToken, serverToken);
}
