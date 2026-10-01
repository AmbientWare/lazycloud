import { queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import type { CustomDomain, CustomDomainList } from "@/lib/api/schemas";
import { accountQueryKeys } from "./workspace-keys";

function viewDomain(domain: Schemas["Domain"]): CustomDomain {
  return {
    ...domain,
    error_code: domain.error_code ?? null,
    error_message: domain.error_message ?? null,
    verified_at: domain.verified_at ?? null,
    last_checked_at: domain.last_checked_at ?? null,
  };
}

export function customDomainsQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.domains(),
    // One page of the API's largest size: the settings list has no paging control.
    queryFn: async (): Promise<CustomDomainList> => {
      const page = await ok(api.GET("/v1/domains", { params: { query: { limit: 100 } } }));
      return { data: page.data.map(viewDomain), next: page.next ?? "" };
    },
    // The server re-reads unsettled domains from the edge on every read, so a short
    // window is what makes a certificate appear without the page being reloaded.
    staleTime: 15_000,
  });
}

export async function registerCustomDomain(domain: string): Promise<CustomDomain> {
  return viewDomain(await ok(api.POST("/v1/domains", { body: { hostname: domain.trim() } })));
}

export async function removeCustomDomain(hostname: string): Promise<null> {
  await ok(api.DELETE("/v1/domains/{hostname}", { params: { path: { hostname } } }));
  return null;
}
