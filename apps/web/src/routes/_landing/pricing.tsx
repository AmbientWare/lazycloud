import { createFileRoute, Link } from '@tanstack/react-router'
import { getProducts, getMeterPricing } from '@/server/functions'
import {
  ProductsCards,
  PricingTransparency,
  EnterpriseSection,
} from './-components/pricing'

export const Route = createFileRoute('/_landing/pricing')({
  loader: async () => {
    const [products, meterPricing] = await Promise.all([
      getProducts(),
      getMeterPricing().catch(() => null),
    ])
    return { products, meterPricing }
  },
  component: PricingPage,
})

function PricingPage() {
  const { products, meterPricing } = Route.useLoaderData()

  return (
    <div className="flex min-h-screen w-full items-start justify-center px-4 py-12">
      <div className="container mx-auto max-w-6xl">
        <div className="mb-8 flex flex-col items-center space-y-4 text-center">
          <h1 className="text-3xl font-bold tracking-tight sm:text-4xl md:text-5xl lg:text-6xl">
            Choose Your Plan
          </h1>
          <p className="max-w-2xl text-base text-muted-foreground sm:text-lg md:text-xl">
            Start for free and scale as you grow.
          </p>
        </div>

        <ProductsCards products={products ?? []} />

        {meterPricing && <PricingTransparency pricing={meterPricing} />}

        <EnterpriseSection />

        <div className="mt-12 space-y-2 text-center">
          <p className="text-sm text-muted-foreground">
            Need help choosing?{' '}
            <Link to="/support" className="text-lazycloud hover:underline">
              Contact Support
            </Link>
          </p>
        </div>
      </div>
    </div>
  )
}
