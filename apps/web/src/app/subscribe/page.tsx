import { Suspense } from "react";
import Link from "next/link";
import { ProductsCards } from "@/app/(landing)/pricing/_components/products-cards";
import { ProductsCardsSkeleton } from "@/app/(landing)/pricing/_components/products-skeleton";
import { PricingTransparency } from "@/app/(landing)/pricing/_components/pricing-transparency";
import { CustomUserButton } from "@/app/_components/custom-user-button";
import HeaderBar from "@/components/shared/header-bar";

export default function SubscribePage() {
  return (
    <div className="flex min-h-screen flex-col">
      <HeaderBar>
        <CustomUserButton />
      </HeaderBar>

      <div className="flex w-full flex-1 flex-col items-center justify-center px-4 py-12">
        <div className="container mx-auto max-w-6xl">
          {/* Simple Header */}
          <div className="mb-10 text-center">
            <h1 className="mb-3 text-3xl font-bold tracking-tight md:text-4xl">
              Choose Your Plan
            </h1>
            <p className="text-muted-foreground">
              Pick a plan to get started. You only pay for what you use.
            </p>
          </div>

          {/* Pricing Cards */}
          <Suspense fallback={<ProductsCardsSkeleton />}>
            <ProductsCards />
          </Suspense>

          {/* Pricing Transparency - collapsed by default for those who want details */}
          <PricingTransparency />

          {/* Minimal footer */}
          <p className="mt-8 text-center text-sm text-muted-foreground">
            Questions?{" "}
            <Link href="/support" className="text-lazycloud hover:underline">
              Contact support
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
