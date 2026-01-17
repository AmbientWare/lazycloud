import { Suspense } from "react";
import { ProductsCards } from "./_components/products-cards";
import { ProductsCardsSkeleton } from "./_components/products-skeleton";
import { EnterpriseSection } from "./_components/enterprise-section";
import { PricingTransparency } from "./_components/pricing-transparency";

export default function PricingPage() {
  return (
    <div className="flex min-h-screen w-full items-start justify-center px-4 py-12">
      <div className="container mx-auto max-w-7xl">
        <div className="mb-8 flex flex-col items-center space-y-4 text-center">
          <h1 className="text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl lg:text-6xl">
            Choose Your Plan
          </h1>
          <p className="text-muted-foreground max-w-2xl text-base sm:text-lg md:text-xl">
            Start for free and scale as you grow.
          </p>
        </div>

        <Suspense fallback={<ProductsCardsSkeleton />}>
          <ProductsCards />
        </Suspense>

        <PricingTransparency />

        <EnterpriseSection />

        <div className="mt-12 space-y-2 text-center">
          <p className="text-muted-foreground text-sm">
            Need help choosing?{" "}
            <a
              href="/support"
              className="text-lazycloud hover:underline"
            >
              Contact Support
            </a>
          </p>
        </div>
      </div>
    </div>
  );
}
