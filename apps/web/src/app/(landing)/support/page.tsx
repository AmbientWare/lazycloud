"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { StyledButton } from "@/components/shared/styled-button";
import { StyledCard } from "@/components/shared/styled-card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { sendSupportEmail } from "@/actions/email";
import { Loader2, Mail, CheckCircle2 } from "lucide-react";

export default function SupportPage() {
  const [email, setEmail] = useState("");
  const [description, setDescription] = useState("");
  const [honeypot, setHoneypot] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    
    if (honeypot) {
      return;
    }

    if (description.length < 10) {
      setError("Please provide a more detailed description (at least 10 characters)");
      return;
    }

    if (description.length > 5000) {
      setError("Description is too long. Please keep it under 5000 characters.");
      return;
    }

    setIsSubmitting(true);

    try {
      await sendSupportEmail(email, description);
      setSuccess(true);
      setEmail("");
      setDescription("");
      setHoneypot("");
      setTimeout(() => {
        setSuccess(false);
      }, 5000);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to send support request");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center px-4 py-12">
      <div className="container mx-auto max-w-2xl">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6 }}
          className="mb-12 flex flex-col items-center space-y-4 text-center"
        >
          <h1 className="text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
            Contact Support
          </h1>
          <p className="text-muted-foreground max-w-2xl text-lg md:text-xl">
            Have a question or need help? Fill out the form below and we'll get back to you as soon as possible.
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
              <h2 className="mb-2 text-2xl font-semibold">Request Submitted!</h2>
              <p className="text-muted-foreground">
                We've received your support request and will get back to you soon.
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

              <div className="space-y-2">
                <label htmlFor="description" className="text-sm font-medium">
                  Description
                </label>
                <Textarea
                  id="description"
                  placeholder="Please describe your issue or question in detail..."
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  required
                  disabled={isSubmitting}
                  rows={12}
                  className="w-full resize-none min-h-[300px]"
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
                  disabled={isSubmitting || !email || !description}
                  className="w-full"
                  size="lg"
                >
                  {isSubmitting ? (
                    <>
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                      Sending...
                    </>
                  ) : (
                    <>
                      <Mail className="mr-2 h-4 w-4" />
                      Send Support Request
                    </>
                  )}
                </StyledButton>
              </form>
            </StyledCard>
          )}
        </motion.div>
      </div>
    </div>
  );
}
