import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, postJson } from "@/lib/api/client";
import {
  customDomainListSchema,
  customDomainSchema,
  type CustomDomain,
  type CustomDomainList,
} from "@/lib/api/schemas";
import { accountQueryKeys } from "./workspace-keys";

const COLLECTION = "/api/v1/custom-domains";

/** A registered domain may be a wildcard, and `*` is not path-safe. */
function domainPath(hostname: string): string {
  return `${COLLECTION}/${encodeURIComponent(hostname)}`;
}

export function customDomainsQueryOptions() {
  return queryOptions({
    queryKey: accountQueryKeys.domains(),
    queryFn: (): Promise<CustomDomainList> => apiRequest(COLLECTION, customDomainListSchema),
    // The server re-reads unsettled domains from the edge on every list, so a short
    // window is what makes a certificate appear without the page being reloaded.
    staleTime: 15_000,
  });
}

export function registerCustomDomain(domain: string): Promise<CustomDomain> {
  return postJson(COLLECTION, customDomainSchema, { domain: domain.trim() });
}

export function removeCustomDomain(hostname: string): Promise<null> {
  return apiRequest(domainPath(hostname), z.null(), { method: "DELETE" });
}
