import { queryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";

export function pricingCatalogQueryOptions() {
  return queryOptions({
    queryKey: ["pricing-catalog"] as const,
    queryFn: () => ok(api.GET("/v1/pricing")),
    staleTime: 5 * 60_000,
  });
}
