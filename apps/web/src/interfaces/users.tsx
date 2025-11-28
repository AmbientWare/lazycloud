export interface WorkspaceFeatureResponse {
  limit: number;
  deployment_limit: number;
  current_count: number;
}

export interface DeploymentFeatureResponse {
  service_limit: number;
  volume_limit: number;
  network_limit: number;
}

export interface UserFeaturesResponse {
  workspace: WorkspaceFeatureResponse;
  deployment: DeploymentFeatureResponse;
  domain_limit: number;
}

