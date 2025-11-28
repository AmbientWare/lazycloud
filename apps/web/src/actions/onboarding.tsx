"use server";

import { clerkClient } from "@clerk/nextjs/server";
import lazycloudApi from "@/server/lazycloud_api";

export const completeOnboarding = async (
  clerkId: string,
  name: string,
  email: string,
) => {
  // check if user exists in machines api
  try {
    await lazycloudApi.onboardUser(clerkId, name, email);
  } catch (error) {
    // If user is already onboarded in the API, that's okay - continue to update Clerk metadata
    const errorMessage = error instanceof Error ? error.message : String(error);
    console.log("LazyCloud API error:", errorMessage);
    if (
      !errorMessage.includes("already exists") &&
      !errorMessage.includes("already onboarded")
    ) {
      console.error("Failed to onboard user:", errorMessage);
      return { error: "There was an error onboarding the user." };
    }
    console.log(
      "User already onboarded in API, continuing to update Clerk metadata",
    );
  }

  const client = await clerkClient();

  try {
    const res = await client.users.updateUser(clerkId, {
      publicMetadata: {
        onboardingComplete: true,
      },
    });
    console.log("Updated Clerk metadata:", res.publicMetadata);
    return { message: res.publicMetadata };
  } catch (error) {
    console.error("Failed to update Clerk metadata:", error);
    return { error: "There was an error updating the user metadata." };
  }
};
