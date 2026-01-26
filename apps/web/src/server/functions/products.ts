import { createServerFn } from '@tanstack/react-start'
import polarService from '../polar'

export interface PolarProduct {
  id: string
  name: string
  description?: string
  prices: Array<{
    amountType: string
    priceAmount: number
  }>
}

export const getProducts = createServerFn({ method: 'GET' }).handler(
  async (): Promise<PolarProduct[]> => {
    try {
      const { result } = await polarService.listProducts({
        isArchived: false,
      })

      if (!result?.items) {
        throw new Error('Invalid response from Polar API: missing items')
      }

      const products = (result.items as PolarProduct[]).sort((a, b) => {
        const aIsBasic = a.name.toLowerCase().includes('basic')
        const bIsBasic = b.name.toLowerCase().includes('basic')

        if (aIsBasic && !bIsBasic) return -1
        if (!aIsBasic && bIsBasic) return 1

        const aPrice =
          a.prices[0]?.amountType === 'fixed'
            ? (a.prices[0]?.priceAmount ?? 0)
            : 0
        const bPrice =
          b.prices[0]?.amountType === 'fixed'
            ? (b.prices[0]?.priceAmount ?? 0)
            : 0

        return aPrice - bPrice
      })

      return products
    } catch (error) {
      console.error('Error fetching products:', {
        error,
        message: error instanceof Error ? error.message : String(error),
      })
      return []
    }
  },
)
