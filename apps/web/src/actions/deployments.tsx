"use server";

import type {
  DeploymentStatusResponse,
} from "@/interfaces/deployments";
import lazycloudApi from "@/server/lazycloud_api";
import { getAuthToken } from "./utils";

export async function getDeploymentStatus(
  deploymentId: string,
): Promise<DeploymentStatusResponse> {
  const accessToken = await getAuthToken();
  return lazycloudApi.getDeploymentStatus(accessToken, deploymentId);
}
