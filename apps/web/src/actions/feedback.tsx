"use server";

import { z } from "zod";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";

const feedbackTypes = ["bug", "feature", "other"] as const;

const feedbackSchema = z.object({
  feedbackType: z.enum(feedbackTypes),
  message: z
    .string()
    .min(10, "Message must be at least 10 characters")
    .max(5000, "Message is too long (max 5000 characters)"),
});

export async function submitFeedback(feedbackType: string, message: string) {
  const result = feedbackSchema.safeParse({ feedbackType, message });
  if (!result.success) {
    throw new Error(result.error.issues[0]?.message ?? "Invalid request");
  }

  const accessToken = await getAuthToken();

  return lazycloudApi.submitFeedback(
    accessToken,
    result.data.feedbackType,
    result.data.message,
    "web",
  );
}
