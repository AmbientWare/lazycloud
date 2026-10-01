import { queryOptions } from "@tanstack/react-query";

import { apiRequest } from "@/lib/api/unserved";
import { pricingCatalogSchema } from "@/lib/api/schemas";

export function pricingCatalogQueryOptions() {
  return queryOptions({
    queryKey: ["pricing-catalog"] as const,
    queryFn: () => apiRequest("/api/v1/pricing", pricingCatalogSchema),
    staleTime: 5 * 60_000,
  });
}
