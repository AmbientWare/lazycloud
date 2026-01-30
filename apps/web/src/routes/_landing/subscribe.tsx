import { createFileRoute } from '@tanstack/react-router'
import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import { getProducts, createCheckoutUrl } from '@/server/functions'
import type { PolarProduct } from '@/server/functions'
import { ProductCard } from './-components/pricing/product-card'

export const Route = createFileRoute('/_landing/subscribe')({
  ssr: false,
  loader: async () => {
    return await getProducts()
  },
  component: SubscribePage,
})

function SubscribePage() {
  const products = Route.useLoaderData()
  const { user, getAuth } = useAuth()

  const handleSubscribe = async (product: PolarProduct) => {
    if (!user) {
      getAuth({ ensureSignedIn: true })
      return
    }

    try {
      const result = await createCheckoutUrl({
        data: {
          productId: product.id,
        },
      })

      if (result.url) {
        window.location.href = result.url
      }
    } catch (error) {
      console.error('Error creating checkout:', error)
    }
  }

  return (
    <div className="container mx-auto px-4 py-16">
      <div className="mb-12 text-center">
        <h1 className="mb-4 text-4xl font-bold">Choose Your Plan</h1>
        <p className="text-muted-foreground text-xl">
          Select the plan that works best for you.
        </p>
      </div>

      <div className="mx-auto grid max-w-5xl grid-cols-1 gap-8 md:grid-cols-3">
        {products.map((product) => (
          <ProductCard
            key={product.id}
            product={product}
            onButtonClick={handleSubscribe}
            buttonText="Subscribe"
          />
        ))}
      </div>
    </div>
  )
}
