"use client";

import { useEffect, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useUser } from "@clerk/nextjs";
import { completeOnboarding } from "@/actions/onboarding";

function OnboardingContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { user, isLoaded } = useUser();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isLoaded) return;

    if (!user) {
      router.push("/sign-in");
      return;
    }

    if (user.publicMetadata?.onboardingComplete) {
      const returnTo = searchParams.get("returnTo") ?? "/";
      router.push(returnTo);
      return;
    }

    const onboard = async () => {
      try {
        const result = await completeOnboarding(
          user.id,
          user.fullName ?? "",
          user.primaryEmailAddress?.emailAddress ?? "",
        );

        if (result?.error) {
          setError(result.error);
          return;
        }

        await user.reload();

        // Small delay to ensure session token refreshes
        await new Promise((resolve) => setTimeout(resolve, 500));

        const returnTo = searchParams.get("returnTo") ?? "/";
        // Use window.location to force full page reload with fresh session
        window.location.href = returnTo;
      } catch {
        setError("Failed to complete onboarding");
      }
    };

    void onboard();
  }, [isLoaded, user, router, searchParams]);

  if (error) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-red-500">Error</h1>
          <p>{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="text-center">
        <h1 className="mb-4 text-2xl font-bold">Setting up your account...</h1>
        <div className="border-primary mx-auto h-8 w-8 animate-spin rounded-full border-4 border-t-transparent" />
      </div>
    </div>
  );
}

export default function OnboardingPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center">
          <div className="text-center">
            <h1 className="mb-4 text-2xl font-bold">Loading...</h1>
            <div className="border-primary mx-auto h-8 w-8 animate-spin rounded-full border-4 border-t-transparent" />
          </div>
        </div>
      }
    >
      <OnboardingContent />
    </Suspense>
  );
}
