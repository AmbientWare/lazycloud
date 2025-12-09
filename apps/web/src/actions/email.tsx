"use server";

import { z } from "zod";
import resendService from "@/server/resend_service";
import { supportRatelimit } from "@/lib/rate-limit";

const emailSchema = z.email("Invalid email address");

const supportEmailSchema = z.object({
  email: emailSchema,
  description: z
    .string()
    .min(10, "Description must be at least 10 characters")
    .max(5000, "Description is too long (max 5000 characters)")
    .refine(
      (val) => {
        const suspiciousPatterns = [
          /(http|https):\/\//gi,
          /\[url\]/gi,
          /<script/gi,
          /javascript:/gi,
        ];
        return !suspiciousPatterns.some((pattern) => pattern.test(val));
      },
      { message: "Description contains invalid content" }
    ),
});

async function checkEmailRateLimit(email: string): Promise<{ allowed: boolean; message?: string }> {
  if (process.env.NODE_ENV === "development" || !supportRatelimit) {
    return { allowed: true };
  }

  const identifier = `email:${email.toLowerCase()}`;
  const { success } = await supportRatelimit.limit(identifier);

  if (!success) {
    return {
      allowed: false,
      message: "Too many email requests. Please wait before submitting another request.",
    };
  }

  return { allowed: true };
}

export async function requestAccessEmail(email: string) {
  const result = emailSchema.safeParse(email);
  if (!result.success) {
    throw new Error(result.error.issues[0]?.message ?? "Invalid email address");
  }

  const rateLimit = await checkEmailRateLimit(email);
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }

  const title = "The following user has requested access to LazyCloud";
  const body = `Email: ${email}`;
  const subject = "ACCESS REQUESTED";

  return resendService.emailSupport(subject, title, body);
}

export async function sendSupportEmail(email: string, description: string) {
  const result = supportEmailSchema.safeParse({ email, description });
  if (!result.success) {
    throw new Error(result.error.issues[0]?.message ?? "Invalid request");
  }

  const rateLimit = await checkEmailRateLimit(email);
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }

  const subject = "SUPPORT REQUESTED";
  const title = "The following user has requested support:";
  const body = `Email: ${email}\nDescription: ${description}`;

  return resendService.emailSupport(subject, title, body);
}

export async function sendEnterpriseInquiry(
  email: string,
  name?: string,
  workosId?: string,
  message?: string
) {
  const result = emailSchema.safeParse(email);
  if (!result.success) {
    throw new Error(result.error.issues[0]?.message ?? "Invalid email address");
  }

  const rateLimit = await checkEmailRateLimit(email);
  if (!rateLimit.allowed) {
    throw new Error(rateLimit.message ?? "Rate limit exceeded");
  }

  const subject = "ENTERPRISE INQUIRY";
  const title = "The following user has requested Enterprise plan information:";
  let body = `Email: ${email}`;
  if (name) {
    body += `\nName: ${name}`;
  }
  if (workosId) {
    body += `\nWorkOS ID: ${workosId}`;
  }
  if (message?.trim()) {
    body += `\n\nMessage:\n${message.trim()}`;
  }

  return resendService.emailSupport(subject, title, body);
}