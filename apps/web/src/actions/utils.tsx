"use server";

import { withAuth } from "@workos-inc/authkit-nextjs";

export async function getUserId() {
  const { user } = await withAuth({ ensureSignedIn: true });
  return user.id;
}

export async function getAuthToken(): Promise<string> {
  const { accessToken } = await withAuth({ ensureSignedIn: true });
  if (!accessToken) {
    throw new Error("No access token available");
  }
  return accessToken;
}
