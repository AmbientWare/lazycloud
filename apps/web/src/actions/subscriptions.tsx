"use server";

// Mock data - in a real application, this would come from an API or database
const remainingCredits = 1000;

export async function getSubscriptionData() {
  return {
    remainingCredits,
  };
}
