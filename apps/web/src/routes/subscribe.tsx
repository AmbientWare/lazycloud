import { createFileRoute } from '@tanstack/react-router'
import { useAuth } from '@workos/authkit-tanstack-react-start/client'
import { getProducts, createCheckoutUrl } from '@/server/functions'
import type { PolarProduct } from '@/server/functions'

export const Route = createFileRoute('/subscribe')({
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
    <div className="container mx-auto py-16 px-4">
      <div className="text-center mb-12">
        <h1 className="text-4xl font-bold mb-4">Choose Your Plan</h1>
        <p className="text-xl text-muted-foreground">
          Select the plan that works best for you.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-5xl mx-auto">
        {products.map((product) => (
          <div
            key={product.id}
            className="rounded-xl border bg-card p-6 flex flex-col"
          >
            <h2 className="text-2xl font-bold mb-2">{product.name}</h2>
            {product.description && (
              <p className="text-muted-foreground mb-4">
                {product.description}
              </p>
            )}
            {product.prices[0] && (
              <div className="text-3xl font-bold mb-6">
                ${(product.prices[0].priceAmount / 100).toFixed(2)}
                <span className="text-sm font-normal text-muted-foreground">
                  /month
                </span>
              </div>
            )}
            <button
              onClick={() => handleSubscribe(product)}
              className="mt-auto w-full bg-primary text-primary-foreground py-2 px-4 rounded-lg hover:bg-primary/90 transition-colors"
            >
              Subscribe
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
