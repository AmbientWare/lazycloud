export interface UserFeaturesResponse {
  deployment_limit: number
  deployment_count: number
  max_team_members: number | null
  max_cpu_per_service: number
  max_memory_per_service: number
  max_replicas_per_service: number
  custom_domains_enabled: boolean
}
