import { queryOptions } from "@tanstack/react-query";

import type { Deployment } from "@/lib/api/schemas";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

/*
 * Pods and devboxes have no public API yet; the workloads packet adds them.
 * Their panels keep the shapes they render, and every read or action answers
 * with this error until then.
 */
const NO_API = "Pods and devboxes have no API yet";

function noApi(): Promise<never> {
  return Promise.reject(new Error(NO_API));
}

export type DevboxPhase =
  | "stopped"
  | "queued"
  | "pulling_image"
  | "restoring_disk"
  | "starting"
  | "running"
  | "stopping"
  | "failed";

export type Devbox = {
  ssh_command: string;
  ssh_host: string;
  state: "running" | "starting" | "stopped";
  phase: DevboxPhase;
  phase_reason: string;
  container_id: string | null;
  failed_container_id: string | null;
  open_connections: number;
  idle_deadline: string | null;
  disk: { size_bytes: number } | null;
};

export function devboxQueryOptions(workspace: string, deploymentId: string) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.deployments.root(workspace), "devbox", deploymentId],
    queryFn: (): Promise<Devbox> => noApi(),
    retry: false,
  });
}

/** A Pod's current deployment: its version, resources and replica bounds. */
export function podDeploymentQueryOptions(workspace: string, app: string, name: string) {
  return queryOptions({
    queryKey: [...workspaceQueryKeys.deployments.root(workspace), "pod", app, name],
    queryFn: (): Promise<Deployment> => noApi(),
    retry: false,
  });
}

export const startDevboxMutationOptions = { mutationFn: (): Promise<Devbox> => noApi() };

export const stopDevboxMutationOptions = { mutationFn: (): Promise<Devbox> => noApi() };

export const scaleDeploymentMutationOptions = { mutationFn: (): Promise<null> => noApi() };
