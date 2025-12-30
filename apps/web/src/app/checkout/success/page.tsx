"use client";

import { useEffect, useRef } from "react";
import { useAuth } from "@workos-inc/authkit-nextjs/components";
import { USER_HOME } from "@/lib/constants";
import { invalidateSubscriptionCacheAction } from "@/actions/subscription";

export default function CheckoutSuccessPage() {
  const { user } = useAuth();
  const hasInvalidated = useRef(false);

  useEffect(() => {
    const handleSuccess = async () => {
      if (!user || hasInvalidated.current) return;
      hasInvalidated.current = true;

      try {
        // Invalidate the subscription cache so middleware sees the new status
        await invalidateSubscriptionCacheAction();
      } catch (error) {
        console.error("Failed to invalidate subscription cache:", error);
      }

      window.location.href = USER_HOME;
    };

    void handleSuccess();
  }, [user]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="space-y-4 text-center">
        <h1 className="text-2xl font-bold">Subscription Successful! 🎉</h1>
        <p className="text-muted-foreground">
          Redirecting to your dashboard...
        </p>
        <div className="border-primary mx-auto mt-4 h-8 w-8 animate-spin rounded-full border-4 border-t-transparent" />
      </div>
    </div>
  );
}
