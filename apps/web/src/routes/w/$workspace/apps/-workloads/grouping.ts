import type { Deployment } from "@/lib/api/schemas";

export type WorkloadGroup = {
  name: string;
  kind: Deployment["kind"];
  active: boolean;
  /** Retained pages of version history. */
  deployments: Deployment[];
  /** Current version, independent of the history window. */
  latest: Deployment;
  stubIds: string[];
};
