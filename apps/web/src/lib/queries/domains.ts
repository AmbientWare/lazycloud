import { queryOptions } from "@tanstack/react-query";
import { z } from "zod";

import { apiRequest, postJson, withWorkspace } from "@/lib/api/client";
import {
  customDomainListSchema,
  customDomainSchema,
  type CustomDomain,
  type CustomDomainList,
} from "@/lib/api/schemas";

const COLLECTION = "/api/v1/custom-domains";

/** A registered domain may be a wildcard, and `*` is not path-safe. */
function domainPath(hostname: string): string {
  return `${COLLECTION}/${encodeURIComponent(hostname)}`;
}

export function customDomainsQueryOptions(workspaceId: string) {
  return queryOptions({
    queryKey: ["custom-domains", workspaceId],
    queryFn: (): Promise<CustomDomainList> =>
      apiRequest(withWorkspace(COLLECTION, workspaceId), customDomainListSchema),
    // The server re-reads unsettled domains from the edge on every list, so a short
    // window is what makes a certificate appear without the page being reloaded.
    staleTime: 15_000,
  });
}

export function registerCustomDomain(workspaceId: string, domain: string): Promise<CustomDomain> {
  return postJson(withWorkspace(COLLECTION, workspaceId), customDomainSchema, {
    domain: domain.trim(),
  });
}

export function removeCustomDomain(workspaceId: string, hostname: string): Promise<null> {
  return apiRequest(withWorkspace(domainPath(hostname), workspaceId), z.null(), {
    method: "DELETE",
  });
}
