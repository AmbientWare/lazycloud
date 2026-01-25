"use server";

import { withAuth } from "@workos-inc/authkit-nextjs";
import { env } from "@/env";
import lazycloudApi from "@/server/lazycloud_api";

// Check if dev auth bypass is enabled
const isDevBypass = () =>
  env.DEV_API_KEY && process.env.NODE_ENV === "development";

// Cache for dev user to avoid repeated API calls
let cachedDevUser: { id: string } | null = null;

export async function getAuthToken(): Promise<string> {
  // Dev auth bypass - return API key instead of WorkOS token
  if (isDevBypass()) {
    return env.DEV_API_KEY!;
  }

  const { accessToken } = await withAuth({ ensureSignedIn: true });
  if (!accessToken) {
    throw new Error("No access token available");
  }
  return accessToken;
}

export async function getUser(): Promise<{ id: string }> {
  // Dev auth bypass - fetch real user from API using the dev API key
  if (isDevBypass()) {
    if (!cachedDevUser) {
      const currentUser = await lazycloudApi.getCurrentUser(env.DEV_API_KEY!);
      cachedDevUser = { id: currentUser.workos_id };
    }
    return cachedDevUser;
  }

  const { user } = await withAuth({ ensureSignedIn: true });
  return user;
}
