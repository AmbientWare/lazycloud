import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok } from "@/lib/api/client";

import { nextListCursor } from "./infinite-list";
import { accountQueryKeys } from "./workspace-keys";

/** The account's domains in hostname order, a page at a time. */
export function customDomainsQueryOptions() {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.domains(),
    initialPageParam: "",
    queryFn: async ({ pageParam }) => {
      const page = await ok(
        api.GET("/v1/domains", { params: { query: { after: pageParam || undefined } } }),
      );
      return { data: page.data, next: page.next ?? "" };
    },
    getNextPageParam: nextListCursor,
    // The server re-reads unsettled domains from the edge on every read, so a short
    // window is what makes a certificate appear without the page being reloaded.
    staleTime: 15_000,
  });
}

export function registerCustomDomain(hostname: string) {
  return ok(api.POST("/v1/domains", { body: { hostname: hostname.trim() } }));
}

export function removeCustomDomain(hostname: string) {
  return ok(api.DELETE("/v1/domains/{hostname}", { params: { path: { hostname } } }));
}
