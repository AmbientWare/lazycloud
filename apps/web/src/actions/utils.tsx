"use server";

import { withAuth } from "@workos-inc/authkit-nextjs";

export async function getUserId() {
  const { user } = await withAuth({ ensureSignedIn: true });
  return user.id;
}
