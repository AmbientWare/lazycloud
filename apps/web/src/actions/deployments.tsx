"use server";

import type {
  DeploymentStatusResponse,
} from "@/interfaces/deployments";
import lazycloudApi from "@/server/lazycloud_api";
import { getUserId } from "./utils";

export async function getDeploymentStatus(
  deploymentId: string,
): Promise<DeploymentStatusResponse> {
  const userId = await getUserId();
  return lazycloudApi.getDeploymentStatus(userId, deploymentId);
}
