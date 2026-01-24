"use server";

import { z } from "zod";
import resendService from "@/server/resend_service";
import { supportRatelimit } from "@/lib/rate-limit";
import { withAuth } from "@workos-inc/authkit-nextjs";

const feedbackTypes = ["bug", "feature", "other"] as const;

const feedbackSchema = z.object({
  feedbackType: z.enum(feedbackTypes),
  message: z
    .string()
    .min(10, "Message must be at least 10 characters")
    .max(5000, "Message is too long (max 5000 characters)")
    .refine(
      (val) => {
        const suspiciousPatterns = [
          /<script/gi,
          /javascript:/gi,
        ];
        return !suspiciousPatterns.some((pattern) => pattern.test(val));
      },
      { message: "Message contains invalid content" }
    ),
});

async function checkFeedbackRateLimit(email: string): Promise<{ allowed: boolean; message?: string }> {
  if (process.env.NODE_ENV === "development" || !supportRatelimit) {
    return { allowed: true };
  }

  const identifier = `feedback:${email.toLowerCase()}`;
  const { success } = await supportRatelimit.limit(identifier);

  if (!success) {
    return {
      allowed: false,
      message: "Too many feedback requests. Please wait before submitting again.",
    };
  }

  return { allowed: true };
}

export async function submitFeedback(feedbackType: string, message: string) {
  const result = feedbackSchema.safeParse({ feedbackType, message });
  if (!result.success) {
    throw new Error(result.error.issues[0]?.message ?? "Invalid request");
  }

  const { user } = await withAuth({ ensureSignedIn: true });

  const rateLimit = await checkFeedbackRateLimit(user.email ?? "unknown");
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }

  const typeLabels: Record<string, string> = {
    bug: "Bug Report",
    feature: "Feature Request",
    other: "General Feedback",
  };
  const typeLabel = typeLabels[result.data.feedbackType] ?? "Feedback";

  const userName = user.firstName && user.lastName
    ? `${user.firstName} ${user.lastName}`
    : user.email;

  const subject = `[WEB] ${typeLabel} from ${user.email}`;
  const title = `[WEB] ${typeLabel} from ${userName} (${user.email})`;
  const body = `Type: ${typeLabel}\n\n${result.data.message}`;

  return resendService.emailSupport(subject, title, body);
}
