"use client";

import { useEffect } from "react";
import { useUser } from "@clerk/nextjs";
import { USER_HOME } from "@/lib/constants";

export default function CheckoutSuccessPage() {
  const { user } = useUser();

  useEffect(() => {
    const handleSuccess = async () => {
      if (!user) return;

      await user.reload();

      await new Promise((resolve) => setTimeout(resolve, 500));

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
