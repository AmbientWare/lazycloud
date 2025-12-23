"use server";

import polarService from "@/server/polar";
import { cacheLife } from "next/cache";
import { connection } from 'next/server'

export interface PolarProduct {
  id: string;
  name: string;
  description?: string;
  prices: Array<{
    amountType: string;
    priceAmount: number;
  }>;
}

async function getProductsWithCache(): Promise<PolarProduct[]> {
  'use cache'
  cacheLife({ expire: 60 }) // 1 minute

  try {
    const { result } = await polarService.listProducts({
      isArchived: false,
    });

    if (!result?.items) {
      throw new Error("Invalid response from Polar API: missing items");
    }

    const products = (result.items as PolarProduct[]).sort((a, b) => {
      const aIsBasic = a.name.toLowerCase().includes("basic");
      const bIsBasic = b.name.toLowerCase().includes("basic");

      if (aIsBasic && !bIsBasic) return -1;
      if (!aIsBasic && bIsBasic) return 1;

      const aPrice =
        a.prices[0]?.amountType === "fixed"
          ? (a.prices[0]?.priceAmount ?? 0)
          : 0;
      const bPrice =
        b.prices[0]?.amountType === "fixed"
          ? (b.prices[0]?.priceAmount ?? 0)
          : 0;

      return aPrice - bPrice;
    });

    return products;
  } catch (error) {
    // Log the error with more details
    console.error("Error in fetchProductsInternal:", {
      error,
      message: error instanceof Error ? error.message : String(error),
      stack: error instanceof Error ? error.stack : undefined,
    });
    // Throw to prevent caching errors
    throw error;
  }
}

export async function getProducts(): Promise<PolarProduct[]> {
  try {
    // connection() rejects during prerendering - this is expected behavior
    // The route will be dynamic at runtime, allowing cache to work
    try {
      await connection();
    } catch (error) {
      if (error instanceof Error && error.message.includes('prerendering')) {
        // Silently continue - prerendering will fail but runtime will work
      } else {
        // Re-throw unexpected errors
        throw error;
      }
    }
    return await getProductsWithCache();
  } catch (error) {
    console.error("Failed to fetch products in getProducts:", {
      error,
      message: error instanceof Error ? error.message : String(error),
    });
    return [];
  }
}
