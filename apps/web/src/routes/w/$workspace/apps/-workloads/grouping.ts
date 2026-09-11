import type { Deployment } from "@/lib/api/schemas";

/**
 * A deployed workload: every deployment version of one kind and name within an
 * app, folded into a single record. Unversioned invokes always route to the
 * newest version (older versions stay invokable at their versioned URLs while
 * active), so the newest version carries the canonical stub used for invoke
 * URLs, scoped logs, and metrics.
 */
export type WorkloadGroup = {
  name: string;
  kind: string;
  /** Whether the newest version — the unversioned invoke target — is active. */
  active: boolean;
  /** All versions, newest first. */
  deployments: Deployment[];
  /** Highest version row: the version unversioned invokes route to. */
  latest: Deployment;
  /** Stub ids across every version, for container and task matching. */
  stubIds: string[];
};

/** The version unversioned invokes route to: always the newest, active or not. */
export function currentDeployment(group: WorkloadGroup): Deployment {
  return group.latest;
}

export function groupDeploymentsByWorkload(
  deployments: Deployment[] | undefined,
  appId: string,
): WorkloadGroup[] {
  const byWorkload = new Map<string, Deployment[]>();
  for (const deployment of deployments ?? []) {
    if (deployment.app_id !== appId) continue;
    const key = JSON.stringify([deployment.kind, deployment.name]);
    const versions = byWorkload.get(key);
    if (versions) versions.push(deployment);
    else byWorkload.set(key, [deployment]);
  }

  const groups: WorkloadGroup[] = [];
  for (const versions of byWorkload.values()) {
    versions.sort((a, b) => b.version - a.version);
    const latest = versions[0];
    groups.push({
      name: latest.name,
      kind: latest.kind,
      active: latest.active,
      deployments: versions,
      latest,
      stubIds: versions
        .map((item) => item.stub_id)
        .filter((stubId): stubId is string => Boolean(stubId)),
    });
  }

  groups.sort((a, b) => b.latest.created_at.localeCompare(a.latest.created_at));
  return groups;
}

export function findWorkloadGroup(
  deployments: Deployment[] | undefined,
  appId: string,
  kind: string,
  name: string,
): WorkloadGroup | undefined {
  return groupDeploymentsByWorkload(deployments, appId).find(
    (group) => group.kind === kind && group.name === name,
  );
}
