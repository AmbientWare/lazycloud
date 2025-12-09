import { Suspense } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { ProductsCards } from "@/app/(landing)/pricing/_components/products-cards";
import { ProductsCardsSkeleton } from "@/app/(landing)/pricing/_components/products-skeleton";
import { EnterpriseSection } from "@/app/(landing)/pricing/_components/enterprise-section";
import { PricingTransparency } from "@/app/(landing)/pricing/_components/pricing-transparency";
import { CustomUserButton } from "@/app/_components/custom-user-button";
import HeaderBar from "@/components/shared/header-bar";

export default function SubscribePage() {
  return (
    <div className="flex min-h-screen flex-col">
      <HeaderBar>
        <CustomUserButton />
      </HeaderBar>

      <div className="flex min-h-screen w-full items-start justify-center px-4 py-12">
        <div className="container mx-auto max-w-7xl">
          <div className="mb-8 flex flex-col items-center space-y-4 text-center">
            <Badge variant="secondary" className="bg-lazycloud mb-2 text-xs">
              Subscription Required
            </Badge>
            <h1 className="text-4xl font-bold tracking-tight md:text-5xl lg:text-6xl">
              Subscribe to Continue
            </h1>
            <p className="text-muted-foreground max-w-2xl text-lg md:text-xl">
              All plans include usage-based pricing. Choose a tier that fits your needs and only pay for what you use.
            </p>
          </div>

          <Suspense fallback={<ProductsCardsSkeleton />}>
            <ProductsCards />
          </Suspense>

          <PricingTransparency />

          <EnterpriseSection />

          <div className="mt-12 space-y-2 text-center">
            <p className="text-muted-foreground text-sm">
              Questions about pricing or features?{" "}
              <a
                href="/support"
                className="text-lazycloud hover:underline"
              >
                Contact Support
              </a>
            </p>
            <p className="text-muted-foreground text-sm">
              <Link
                href="/pricing"
                className="text-lazycloud hover:underline"
              >
                View detailed pricing information
              </Link>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
