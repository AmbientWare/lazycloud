"use client";

import { useState, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { motion } from "framer-motion";
import { StyledButton } from "@/components/shared/styled-button";
import { StyledCard } from "@/components/shared/styled-card";
import { Input } from "@/components/ui/input";
import { requestAccessEmail } from "@/actions/email";
import { Loader2, Zap, CheckCircle2 } from "lucide-react";
import Link from "next/link";

const AUTH_ERROR_MESSAGES: Record<string, string> = {
  access_denied: "Access denied. You may not have permission to sign in yet.",
  auth_failed: "Authentication failed. Please try again or request access below.",
};

export default function RequestAccessPage() {
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [honeypot, setHoneypot] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    const authError = searchParams.get("error");
    if (authError && AUTH_ERROR_MESSAGES[authError]) {
      setError(AUTH_ERROR_MESSAGES[authError]);
    }
  }, [searchParams]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (honeypot) {
      return;
    }

    setIsSubmitting(true);

    try {
      await requestAccessEmail(email);
      setSuccess(true);
      setEmail("");
      setHoneypot("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send request");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center px-4 py-12">
      <div className="container mx-auto max-w-xl">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          className="mb-12 flex flex-col items-center space-y-4 text-center"
        >
          <h1 className="text-4xl font-bold tracking-tight md:text-5xl">
            Request Access
          </h1>
          <p className="text-muted-foreground max-w-lg text-lg">
            LazyCloud is currently invite-only. Enter your email below and
            we&apos;ll reach out when we&apos;re ready for you.
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.1 }}
        >
          {success ? (
            <StyledCard variant="static" className="p-8 text-center">
              <CheckCircle2 className="mx-auto mb-4 h-12 w-12 text-green-600" />
              <h2 className="mb-2 text-2xl font-semibold">You&apos;re on the list!</h2>
              <p className="text-muted-foreground">
                We&apos;ll review your request and send an invite when a spot opens up.
              </p>
            </StyledCard>
          ) : (
            <StyledCard variant="static" className="p-8">
              <form onSubmit={handleSubmit} className="space-y-6">
                <input
                  type="text"
                  name="website"
                  value={honeypot}
                  onChange={(e) => setHoneypot(e.target.value)}
                  style={{ display: "none" }}
                  tabIndex={-1}
                  autoComplete="off"
                  aria-hidden="true"
                />
                <div className="space-y-2">
                  <label htmlFor="email" className="text-sm font-medium">
                    Email Address
                  </label>
                  <Input
                    id="email"
                    type="email"
                    placeholder="your.email@example.com"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                    disabled={isSubmitting}
                    className="w-full"
                  />
                </div>

                {error && (
                  <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive">
                    {error}
                  </div>
                )}

                <StyledButton
                  type="submit"
                  variant="primary"
                  disabled={isSubmitting || !email}
                  className="w-full"
                  size="lg"
                >
                  {isSubmitting ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Submitting...
                    </>
                  ) : (
                    <>
                      <Zap className="mr-2 h-4 w-4" />
                      Request Access
                    </>
                  )}
                </StyledButton>
              </form>

              <p className="mt-6 text-center text-sm text-muted-foreground">
                Already have an account?{" "}
                <Link
                  href="/login"
                  prefetch={false}
                  className="text-foreground underline hover:text-foreground/80"
                >
                  Sign in
                </Link>
              </p>
            </StyledCard>
          )}
        </motion.div>
      </div>
    </div>
  );
}
