{{- define "lazycloud.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else if contains (include "lazycloud.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "lazycloud.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.labels" -}}
app.kubernetes.io/name: {{ include "lazycloud.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "lazycloud.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lazycloud.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "lazycloud.secretRolloutAnnotations" -}}
{{- if .Values.config.create }}
checksum/lazycloud-config: {{ include (print .Template.BasePath "/secret.yaml") . | sha256sum | quote }}
{{- end }}
{{- with .Values.config.externalSecretRevision }}
lazycloud.io/external-secret-revision: {{ . | quote }}
{{- end }}
{{- end -}}

{{- define "lazycloud.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "lazycloud.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.providerServiceAccountName" -}}
{{- if .Values.providerServiceAccount.create -}}
{{- default (printf "%s-provider" (include "lazycloud.fullname" .)) .Values.providerServiceAccount.name -}}
{{- else -}}
{{- default (include "lazycloud.serviceAccountName" .) .Values.providerServiceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.workerTokenServiceAccountName" -}}
{{- if .Values.rbac.create -}}
{{- printf "%s-worker-token" (include "lazycloud.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "lazycloud.serviceAccountName" . -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.administratorBootstrapServiceAccountName" -}}
{{- if .Values.rbac.create -}}
{{- printf "%s-administrator-bootstrap" (include "lazycloud.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- include "lazycloud.serviceAccountName" . -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.administratorCredentialSecretName" -}}
{{- default (printf "%s-administrator" (include "lazycloud.fullname" .)) .Values.administratorBootstrap.credentialSecretName | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.containerWorkerName" -}}
{{- $root := index . "root" -}}
{{- $poolName := required "containerWorkers.pools[].name is required" (index . "poolName") -}}
{{- $poolKey := $poolName | lower | replace "_" "-" -}}
{{- $untruncated := printf "%s-container-worker-%s" (include "lazycloud.fullname" $root) $poolKey -}}
{{- $name := $untruncated | trunc 63 | trimSuffix "-" -}}
{{- if not (hasSuffix $poolKey $name) -}}
{{- fail (printf "container-worker Deployment name would truncate worker pool identity %q" $poolName) -}}
{{- end -}}
{{- $name -}}
{{- end -}}

{{- define "lazycloud.validateContainerWorkerPools" -}}
{{- $names := dict -}}
{{- $owners := dict -}}
{{- range $pool := .Values.containerWorkers.pools -}}
{{- $name := required "containerWorkers.pools[].name is required" $pool.name -}}
{{- $owner := required "containerWorkers.pools[].capacityOwnerId is required" $pool.capacityOwnerId -}}
{{- if hasKey $names $name -}}
{{- fail (printf "containerWorkers pool name %q must be unique" $name) -}}
{{- end -}}
{{- if hasKey $owners $owner -}}
{{- fail (printf "containerWorkers capacityOwnerId %q must be unique" $owner) -}}
{{- end -}}
{{- $_ := set $names $name true -}}
{{- $_ := set $owners $owner true -}}
{{- $initial := int $pool.initialWorkers -}}
{{- $minimum := int $pool.minWorkers -}}
{{- $maximum := int $pool.maxWorkers -}}
{{- if or (lt $initial $minimum) (gt $initial $maximum) -}}
{{- fail (printf "containerWorkers pool %q must satisfy minWorkers <= initialWorkers <= maxWorkers" $name) -}}
{{- end -}}
{{- if and $pool.scalingEnabled (eq $maximum 0) -}}
{{- fail (printf "containerWorkers pool %q cannot enable scaling with maxWorkers=0" $name) -}}
{{- end -}}
{{- if not (has $pool.runtime $pool.runtimes) -}}
{{- fail (printf "containerWorkers pool %q runtime must be present in runtimes" $name) -}}
{{- end -}}
{{- if and $pool.gpu.enabled (lt (int $pool.gpu.count) 1) -}}
{{- fail (printf "containerWorkers pool %q must advertise at least one GPU when GPU is enabled" $name) -}}
{{- end -}}
{{- if and (not $pool.gpu.enabled) (ne (int $pool.gpu.count) 0) -}}
{{- fail (printf "containerWorkers pool %q must advertise zero GPUs when GPU is disabled" $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.kubernetesCapacityPools" -}}
{{- include "lazycloud.validateContainerWorkerPools" . -}}
{{- $policies := list -}}
{{- range $pool := .Values.containerWorkers.pools -}}
{{- $gpuType := "" -}}
{{- $gpuCount := 0 -}}
{{- if $pool.gpu.enabled -}}
{{- $gpuType = $pool.gpu.type -}}
{{- $gpuCount = int $pool.gpu.count -}}
{{- end -}}
{{- $policy := dict
      "capacity_owner_id" $pool.capacityOwnerId
      "capacity_owner_kind" "global_kubernetes_deployment"
      "capacity_owner_source" "kubernetes"
      "pool_name" $pool.name
      "initial_workers" (int $pool.initialWorkers)
      "min_workers" (int $pool.minWorkers)
      "max_workers" (int $pool.maxWorkers)
      "scaling_enabled" $pool.scalingEnabled
      "default_eligible" $pool.defaultEligible
      "priority" (int $pool.priority)
      "min_free_cpu_millicores" (int $pool.minFreeCpuMillicores)
      "min_free_memory_mib" (int $pool.minFreeMemoryMib)
      "min_free_gpu_count" (int $pool.minFreeGpuCount)
      "worker_cpu_millicores" (int $pool.capacity.cpuMillicores)
      "worker_memory_mib" (int $pool.capacity.memoryMib)
      "worker_gpu_type" $gpuType
      "worker_gpu_count" $gpuCount
      "worker_runtimes" $pool.runtimes
      "worker_preemptible" $pool.preemptible
      "idle_drain_timeout_seconds" (int $pool.idleDrainTimeoutSeconds)
      "scale_up_cooldown_seconds" (int $pool.scaleUpCooldownSeconds)
      "scale_down_cooldown_seconds" (int $pool.scaleDownCooldownSeconds)
      "registration_timeout_seconds" (int $pool.registrationTimeoutSeconds)
  -}}
{{- $policies = append $policies $policy -}}
{{- end -}}
{{- $policies | toJson -}}
{{- end -}}

{{- define "lazycloud.hasKubernetesCapacityScaling" -}}
{{- $enabled := false -}}
{{- range $pool := .Values.containerWorkers.pools -}}
{{- if $pool.scalingEnabled -}}
{{- $enabled = true -}}
{{- end -}}
{{- end -}}
{{- $enabled -}}
{{- end -}}

{{- define "lazycloud.configSecretName" -}}
{{- default (printf "%s-config" (include "lazycloud.fullname" .)) .Values.config.existingSecret -}}
{{- end -}}

{{- define "lazycloud.cacheServiceSecretName" -}}
{{- default (printf "%s-cache-service" (include "lazycloud.fullname" .)) .Values.cache.existingSecret -}}
{{- end -}}

{{- define "lazycloud.postgresqlName" -}}
{{- default (printf "%s-postgresql" (include "lazycloud.fullname" .)) .Values.postgresql.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.redisName" -}}
{{- default (printf "%s-redis" (include "lazycloud.fullname" .)) .Values.redisStateful.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.objectStoreName" -}}
{{- default (printf "%s-object-store" (include "lazycloud.fullname" .)) .Values.objectStore.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.juicefsGatewayName" -}}
{{- printf "%s-juicefs-gateway" (include "lazycloud.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "lazycloud.databaseUrl" -}}
{{- if .Values.postgresql.enabled -}}
{{- $password := required "postgresql.password is required when the bundled PostgreSQL service is enabled" .Values.postgresql.password -}}
{{- printf "postgresql+psycopg://%s:%s@%s:5432/%s" (.Values.postgresql.username | urlquery) ($password | urlquery) (include "lazycloud.postgresqlName" .) (.Values.postgresql.database | urlquery) -}}
{{- else -}}
{{- fail "config.create requires postgresql.enabled=true; external database URLs must come from database.secretRef" -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.redisUrl" -}}
{{- if .Values.redisStateful.enabled -}}
{{- printf "redis://%s:6379/0" (include "lazycloud.redisName" .) -}}
{{- else -}}
{{- fail "config.create requires redisStateful.enabled=true; external Redis URLs must come from redis.secretRef" -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.databaseSecretName" -}}
{{- default (include "lazycloud.configSecretName" .) .Values.database.secretRef.name -}}
{{- end -}}

{{- define "lazycloud.redisSecretName" -}}
{{- default (include "lazycloud.configSecretName" .) .Values.redis.secretRef.name -}}
{{- end -}}

{{- define "lazycloud.objectStoreCredentialsSecretName" -}}
{{- default (include "lazycloud.configSecretName" .) .Values.objectStore.credentialsSecretRef.name -}}
{{- end -}}

{{- define "lazycloud.juicefsGatewayCredentialsSecretName" -}}
{{- default (include "lazycloud.configSecretName" .) .Values.juicefs.gateway.credentialsSecretRef.name -}}
{{- end -}}

{{- define "lazycloud.juicefsMetadataSecretName" -}}
{{- default (include "lazycloud.configSecretName" .) .Values.juicefs.metadataSecretRef.name -}}
{{- end -}}

{{- define "lazycloud.juicefsMetadataUrl" -}}
{{- if .Values.juicefs.metadataUrl -}}
{{- .Values.juicefs.metadataUrl -}}
{{- else if .Values.redisStateful.enabled -}}
{{- printf "redis://%s:6379/1" (include "lazycloud.redisName" .) -}}
{{- else -}}
{{- required "juicefs.metadataUrl is required when bundled Redis is disabled" .Values.juicefs.metadataUrl -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.juicefsGatewayEndpoint" -}}
{{- if .Values.juicefs.gateway.endpointUrl -}}
{{- .Values.juicefs.gateway.endpointUrl -}}
{{- else if .Values.juicefs.gateway.enabled -}}
{{- printf "http://%s:%v" (include "lazycloud.juicefsGatewayName" .) .Values.juicefs.gateway.port -}}
{{- else -}}
{{- required "juicefs.gateway.endpointUrl is required when the bundled gateway is disabled" .Values.juicefs.gateway.endpointUrl -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.juicefsWebDavEndpoint" -}}
{{- if .Values.juicefs.gateway.webdavEndpointUrl -}}
{{- .Values.juicefs.gateway.webdavEndpointUrl -}}
{{- else if .Values.juicefs.gateway.enabled -}}
{{- printf "http://%s:%v" (include "lazycloud.juicefsGatewayName" .) .Values.juicefs.gateway.webdavPort -}}
{{- else -}}
{{- required "juicefs.gateway.webdavEndpointUrl is required when the bundled gateway is disabled" .Values.juicefs.gateway.webdavEndpointUrl -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.juicefsDataBucketUrl" -}}
{{- printf "%s/%s" (trimSuffix "/" (include "lazycloud.objectStoreEndpoint" .)) .Values.objectStore.dataBucket -}}
{{- end -}}

{{- define "lazycloud.imageArchiveBucket" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- required "imageBuild.archive.backend.bucket is required when the separate archive backend is enabled" .Values.imageBuild.archive.backend.bucket -}}
{{- else -}}
{{- default .Values.objectStore.bucket .Values.imageBuild.archive.bucket -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveEndpoint" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.endpointUrl -}}
{{- else -}}
{{- include "lazycloud.objectStoreEndpoint" . -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchivePresignedEndpoint" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.presignedEndpointUrl -}}
{{- else -}}
{{- default .Values.objectStore.presignedEndpointUrl .Values.imageBuild.archive.presignedEndpointUrl -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveRegion" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.regionName -}}
{{- else -}}
{{- .Values.objectStore.regionName -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveForcePathStyle" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.forcePathStyle -}}
{{- else -}}
{{- .Values.objectStore.forcePathStyle -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveTransferMultipartThresholdBytes" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.transferMultipartThresholdBytes -}}
{{- else -}}
{{- .Values.objectStore.transferMultipartThresholdBytes -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveTransferMultipartChunkSizeBytes" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.transferMultipartChunkSizeBytes -}}
{{- else -}}
{{- .Values.objectStore.transferMultipartChunkSizeBytes -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.imageArchiveTransferMaxConcurrency" -}}
{{- if .Values.imageBuild.archive.backend.enabled -}}
{{- .Values.imageBuild.archive.backend.transferMaxConcurrency -}}
{{- else -}}
{{- .Values.objectStore.transferMaxConcurrency -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.databaseEnv" -}}
- name: LAZYCLOUD_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.databaseSecretName" . | quote }}
      key: {{ .Values.database.secretRef.key | quote }}
      optional: false
- name: LAZYCLOUD_DATABASE_ECHO
  value: {{ .Values.database.echo | quote }}
- name: LAZYCLOUD_DATABASE_POOL_SIZE
  value: {{ .Values.database.poolSize | quote }}
- name: LAZYCLOUD_DATABASE_MAX_OVERFLOW
  value: {{ .Values.database.maxOverflow | quote }}
- name: LAZYCLOUD_DATABASE_CONNECT_TIMEOUT_SECONDS
  value: {{ .Values.database.connectTimeoutSeconds | quote }}
- name: LAZYCLOUD_DATABASE_STATEMENT_TIMEOUT_MS
  value: {{ .Values.database.statementTimeoutMs | quote }}
{{- end -}}

{{- define "lazycloud.redisEnv" -}}
- name: LAZYCLOUD_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.redisSecretName" . | quote }}
      key: {{ .Values.redis.secretRef.key | quote }}
      optional: false
- name: LAZYCLOUD_REDIS_KEY_PREFIX
  value: {{ .Values.redis.keyPrefix | quote }}
{{- if .Values.redis.clientName }}
- name: LAZYCLOUD_REDIS_CLIENT_NAME
  value: {{ .Values.redis.clientName | quote }}
{{- end }}
- name: LAZYCLOUD_REDIS_SOCKET_TIMEOUT_SECONDS
  value: {{ .Values.redis.socketTimeoutSeconds | quote }}
- name: LAZYCLOUD_REDIS_HEALTH_CHECK_INTERVAL_SECONDS
  value: {{ .Values.redis.healthCheckIntervalSeconds | quote }}
{{- end -}}

{{- define "lazycloud.objectStoreEnv" -}}
- name: LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL
  value: {{ include "lazycloud.objectStoreEndpoint" . | quote }}
{{- if .Values.objectStore.presignedEndpointUrl }}
- name: LAZYCLOUD_OBJECT_STORE_PRESIGNED_ENDPOINT_URL
  value: {{ .Values.objectStore.presignedEndpointUrl | quote }}
{{- end }}
- name: LAZYCLOUD_OBJECT_STORE_BUCKET
  value: {{ .Values.objectStore.bucket | quote }}
- name: LAZYCLOUD_OBJECT_STORE_REGION_NAME
  value: {{ .Values.objectStore.regionName | quote }}
- name: LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE
  value: {{ .Values.objectStore.forcePathStyle | quote }}
- name: LAZYCLOUD_OBJECT_STORE_TRANSFER_MULTIPART_THRESHOLD_BYTES
  value: {{ .Values.objectStore.transferMultipartThresholdBytes | quote }}
- name: LAZYCLOUD_OBJECT_STORE_TRANSFER_MULTIPART_CHUNK_SIZE_BYTES
  value: {{ .Values.objectStore.transferMultipartChunkSizeBytes | quote }}
- name: LAZYCLOUD_OBJECT_STORE_TRANSFER_MAX_CONCURRENCY
  value: {{ .Values.objectStore.transferMaxConcurrency | quote }}
- name: LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.objectStoreCredentialsSecretName" . | quote }}
      key: {{ .Values.objectStore.credentialsSecretRef.accessKeyIdKey | quote }}
      optional: true
- name: LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.objectStoreCredentialsSecretName" . | quote }}
      key: {{ .Values.objectStore.credentialsSecretRef.secretAccessKeyKey | quote }}
      optional: true
{{- end -}}

{{- define "lazycloud.juicefsGatewayEnv" -}}
- name: LAZYCLOUD_JUICEFS_GATEWAY_ENDPOINT_URL
  value: {{ include "lazycloud.juicefsGatewayEndpoint" . | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_WEBDAV_ENDPOINT_URL
  value: {{ include "lazycloud.juicefsWebDavEndpoint" . | quote }}
{{- if .Values.juicefs.gateway.presignedEndpointUrl }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_PRESIGNED_ENDPOINT_URL
  value: {{ .Values.juicefs.gateway.presignedEndpointUrl | quote }}
{{- end }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_BUCKET
  value: {{ .Values.juicefs.filesystemName | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_REGION_NAME
  value: {{ .Values.objectStore.regionName | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_FORCE_PATH_STYLE
  value: "true"
- name: LAZYCLOUD_JUICEFS_GATEWAY_TRANSFER_MULTIPART_THRESHOLD_BYTES
  value: {{ .Values.juicefs.gateway.transferMultipartThresholdBytes | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_TRANSFER_MULTIPART_CHUNK_SIZE_BYTES
  value: {{ .Values.juicefs.gateway.transferMultipartChunkSizeBytes | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_TRANSFER_MAX_CONCURRENCY
  value: {{ .Values.juicefs.gateway.transferMaxConcurrency | quote }}
- name: LAZYCLOUD_JUICEFS_GATEWAY_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.juicefsGatewayCredentialsSecretName" . | quote }}
      key: {{ .Values.juicefs.gateway.credentialsSecretRef.accessKeyIdKey | quote }}
      optional: false
- name: LAZYCLOUD_JUICEFS_GATEWAY_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "lazycloud.juicefsGatewayCredentialsSecretName" . | quote }}
      key: {{ .Values.juicefs.gateway.credentialsSecretRef.secretAccessKeyKey | quote }}
      optional: false
{{- end -}}

{{- define "lazycloud.apiEnv" -}}
- name: LAZYCLOUD_LOG_LEVEL
  value: {{ .Values.platform.logLevel | quote }}
- name: LAZYCLOUD_TELEMETRY_ENABLED
  value: {{ .Values.platform.telemetry.enabled | quote }}
- name: LAZYCLOUD_TELEMETRY_ENDPOINT
  value: {{ .Values.platform.telemetry.endpoint | quote }}
- name: LAZYCLOUD_TELEMETRY_METER_INTERVAL_SECONDS
  value: {{ .Values.platform.telemetry.meterIntervalSeconds | quote }}
- name: LAZYCLOUD_TELEMETRY_TRACE_INTERVAL_SECONDS
  value: {{ .Values.platform.telemetry.traceIntervalSeconds | quote }}
- name: LAZYCLOUD_TELEMETRY_TRACE_SAMPLE_RATIO
  value: {{ .Values.platform.telemetry.traceSampleRatio | quote }}
- name: LAZYCLOUD_TELEMETRY_EXPORT_TIMEOUT_SECONDS
  value: {{ .Values.platform.telemetry.exportTimeoutSeconds | quote }}
- name: LAZYCLOUD_TELEMETRY_EXPORT_TRACES
  value: {{ .Values.platform.telemetry.exportTraces | quote }}
- name: LAZYCLOUD_TELEMETRY_EXPORT_METRICS
  value: {{ .Values.platform.telemetry.exportMetrics | quote }}
- name: LAZYCLOUD_TELEMETRY_EXPORT_LOGS
  value: {{ .Values.platform.telemetry.exportLogs | quote }}
- name: LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL
  value: {{ required "controlPlane.publicHttpUrl is required" .Values.controlPlane.publicHttpUrl | quote }}
- name: LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL
  value: {{ .Values.controlPlane.runtimeHttpUrl | default (printf "http://%s:%v" (include "lazycloud.fullname" .) .Values.service.port) | quote }}
- name: LAZYCLOUD_AGENT_ROUTE_RECONCILIATION_INTERVAL_SECONDS
  value: {{ .Values.controlPlane.agentRouteReconciliationIntervalSeconds | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_ENABLED
  value: {{ .Values.awsCapacity.enabled | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_DRAFT_TTL_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.draftTtlSeconds | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CLEANUP_TOMBSTONE_TTL_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.cleanupTombstoneTtlSeconds | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CLEANUP_TIMEOUT_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.cleanupTimeoutSeconds | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_RECONCILIATION_INTERVAL_SECONDS
  value: {{ .Values.awsCapacity.reconciliation.intervalSeconds | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_RECONCILIATION_LIMIT
  value: {{ .Values.awsCapacity.reconciliation.limit | quote }}
{{- if .Values.agentArtifact.version }}
{{- $_ := required "agentArtifact.sha256ByArch.amd64 is required when agentArtifact.version is configured" .Values.agentArtifact.sha256ByArch.amd64 }}
{{- if and .Values.agentArtifact.url .Values.agentArtifact.volume.existingClaim }}
{{- fail "agentArtifact.url and agentArtifact.volume.existingClaim are mutually exclusive" }}
{{- end }}
{{- if not (or .Values.agentArtifact.url .Values.agentArtifact.volume.existingClaim) }}
{{- fail "agentArtifact.url or agentArtifact.volume.existingClaim is required when agentArtifact.version is configured" }}
{{- end }}
{{- if and .Values.agentArtifact.url (not (contains .Values.agentArtifact.sha256ByArch.amd64 .Values.agentArtifact.url)) }}
{{- fail "agentArtifact.url must contain agentArtifact.sha256ByArch.amd64" }}
{{- end }}
- name: LAZYCLOUD_AGENT_BINARY_DIR
  value: {{ .Values.agentArtifact.volume.mountPath | required "agentArtifact.volume.mountPath is required" | quote }}
- name: LAZYCLOUD_AGENT_BINARY_VERSION
  value: {{ .Values.agentArtifact.version | quote }}
- name: LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH
  value: {{ dict "amd64" .Values.agentArtifact.sha256ByArch.amd64 | toJson | quote }}
{{- end }}
{{- if .Values.awsCapacity.enabled }}
{{- if not (hasPrefix "https://" .Values.controlPlane.publicHttpUrl) }}
{{- fail "controlPlane.publicHttpUrl must be an externally reachable HTTPS URL when awsCapacity.enabled is true" }}
{{- end }}
{{- if not .Values.tailnet.enabled }}
{{- fail "tailnet.enabled must be true when awsCapacity.enabled is true" }}
{{- end }}
{{- $_ := required "agentArtifact.version is required when awsCapacity.enabled is true" .Values.agentArtifact.version }}
{{- $_ := required "agentArtifact.sha256ByArch.amd64 is required when awsCapacity.enabled is true" .Values.agentArtifact.sha256ByArch.amd64 }}
{{- $_ := required "tailnet.authKeySecretRef.name is required when awsCapacity.enabled is true" .Values.tailnet.authKeySecretRef.name }}
{{- $_ := required "tailnet.oauthClientId is required when awsCapacity.enabled is true" .Values.tailnet.oauthClientId }}
{{- $_ := required "tailnet.oauthClientSecretRef.name is required when awsCapacity.enabled is true" .Values.tailnet.oauthClientSecretRef.name }}
- name: LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL
  value: {{ .Values.awsCapacity.connectionTemplateUrl | required "awsCapacity.connectionTemplateUrl is required" | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST
  value: {{ .Values.awsCapacity.workerImageDigest | required "awsCapacity.workerImageDigest is required" | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL
  value: {{ .Values.awsCapacity.agentArtifactUrl | required "awsCapacity.agentArtifactUrl is required" | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS
  value: {{ .Values.awsCapacity.cpuAmiIds | required "awsCapacity.cpuAmiIds is required" | toJson | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS
  value: {{ .Values.awsCapacity.gpuAmiIds | required "awsCapacity.gpuAmiIds is required" | toJson | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN
  value: {{ .Values.awsCapacity.controlPrincipalArn | required "awsCapacity.controlPrincipalArn is required" | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_INSTANCE_HOURLY_MICROS
  value: {{ .Values.awsCapacity.instanceHourlyMicros | required "awsCapacity.instanceHourlyMicros is required" | toJson | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_EXTERNAL_ID_BYTES
  value: {{ .Values.awsCapacity.externalIdBytes | quote }}
{{- end }}
{{- if .Values.tailnet.enabled }}
{{- $_ := required "tailnet.authKeySecretRef.name is required when tailnet.enabled is true" .Values.tailnet.authKeySecretRef.name }}
{{- $_ := required "tailnet.oauthClientId is required when tailnet.enabled is true" .Values.tailnet.oauthClientId }}
{{- $_ := required "tailnet.oauthClientSecretRef.name is required when tailnet.enabled is true" .Values.tailnet.oauthClientSecretRef.name }}
{{- $_ := required "tailnet.routeAuthKeySecretRef.name is required when tailnet.enabled is true" .Values.tailnet.routeAuthKeySecretRef.name }}
{{- if eq .Values.tailnet.agentTag .Values.tailnet.controlPlaneTag }}
{{- fail "tailnet.agentTag and tailnet.controlPlaneTag must be distinct" }}
{{- end }}
{{- end }}
- name: LAZYCLOUD_WORKSPACE_CHANGE_STREAM_MAX_LENGTH
  value: {{ .Values.events.workspaceChangeStreamMaxLength | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_EXECUTOR
  value: {{ .Values.imageBuild.executor | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_DOCKER_BINARY
  value: {{ .Values.imageBuild.dockerBinary | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_PUSH_ENABLED
  value: {{ .Values.imageBuild.registryPushEnabled | quote }}
{{- if .Values.imageBuild.registryTargetRef }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_TARGET_REF
  value: {{ .Values.imageBuild.registryTargetRef | quote }}
{{- end }}
{{- if .Values.imageBuild.registryDockerBinary }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_DOCKER_BINARY
  value: {{ .Values.imageBuild.registryDockerBinary | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_INSPECT_BINARY
  value: {{ .Values.imageBuild.registryInspectBinary | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_INSPECT_TIMEOUT_SECONDS
  value: {{ .Values.imageBuild.registryInspectTimeoutSeconds | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_REGISTRY_INSPECT_TLS_VERIFY
  value: {{ .Values.imageBuild.registryInspectTlsVerify | quote }}
{{- if .Values.imageBuild.originRegistry.host }}
- name: LAZYCLOUD_BUILD_REGISTRY
  value: {{ .Values.imageBuild.originRegistry.host | quote }}
{{- end }}
{{- if .Values.imageBuild.originRegistry.credentialsResourceName }}
- name: LAZYCLOUD_BUILD_REGISTRY_CREDENTIALS_SECRET
  value: {{ .Values.imageBuild.originRegistry.credentialsResourceName | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.bucket }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BUCKET
  value: {{ .Values.imageBuild.archive.bucket | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.presignedEndpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PRESIGNED_ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.presignedEndpointUrl | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.prefix }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PREFIX
  value: {{ .Values.imageBuild.archive.prefix | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PRESIGN_SECONDS
  value: {{ .Values.imageBuild.archive.presignSeconds | quote }}
{{- if .Values.imageBuild.archive.backend.enabled }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__BUCKET
  value: {{ include "lazycloud.imageArchiveBucket" . | quote }}
{{- if .Values.imageBuild.archive.backend.endpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.backend.endpointUrl | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.backend.presignedEndpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__PRESIGNED_ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.backend.presignedEndpointUrl | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__REGION_NAME
  value: {{ .Values.imageBuild.archive.backend.regionName | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__FORCE_PATH_STYLE
  value: {{ .Values.imageBuild.archive.backend.forcePathStyle | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MULTIPART_THRESHOLD_BYTES
  value: {{ .Values.imageBuild.archive.backend.transferMultipartThresholdBytes | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MULTIPART_CHUNK_SIZE_BYTES
  value: {{ .Values.imageBuild.archive.backend.transferMultipartChunkSizeBytes | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MAX_CONCURRENCY
  value: {{ .Values.imageBuild.archive.backend.transferMaxConcurrency | quote }}
{{- end }}
{{- if .Values.imageBuild.container.poolSelector }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_POOL_SELECTOR
  value: {{ .Values.imageBuild.container.poolSelector | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_CPU_MILLICORES
  value: {{ .Values.imageBuild.container.cpuMillicores | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_MEMORY_MIB
  value: {{ .Values.imageBuild.container.memoryMib | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_TIMEOUT_SECONDS
  value: {{ .Values.imageBuild.container.addressWaitTimeoutSeconds | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_INTERVAL_SECONDS
  value: {{ .Values.imageBuild.container.addressPollIntervalSeconds | quote }}
- name: LAZYCLOUD_RETENTION_ENABLED
  value: {{ .Values.scheduler.retention.enabled | quote }}
- name: LAZYCLOUD_RETENTION_INTERVAL_SECONDS
  value: {{ .Values.scheduler.retention.intervalSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RETRY_INITIAL_SECONDS
  value: {{ .Values.scheduler.retention.retryInitialSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RETRY_MAX_SECONDS
  value: {{ .Values.scheduler.retention.retryMaxSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RECENT_STUB_TTL_SECONDS
  value: {{ .Values.scheduler.retention.recentStubTtlSeconds | quote }}
- name: LAZYCLOUD_RETENTION_SOURCE_GRACE_SECONDS
  value: {{ .Values.scheduler.retention.sourceGraceSeconds | quote }}
- name: LAZYCLOUD_RETENTION_CHECKPOINT_SECONDS
  value: {{ .Values.scheduler.retention.checkpointSeconds | quote }}
- name: LAZYCLOUD_RETENTION_BUILD_SECONDS
  value: {{ .Values.scheduler.retention.buildSeconds | quote }}
- name: LAZYCLOUD_RETENTION_IMAGE_SECONDS
  value: {{ .Values.scheduler.retention.imageSeconds | quote }}
- name: LAZYCLOUD_RETENTION_MAX_ITEMS_PER_CYCLE
  value: {{ .Values.scheduler.retention.maxItemsPerCycle | quote }}
- name: LAZYCLOUD_USAGE_BILLING_CURRENCY
  value: {{ .Values.usage.billingCurrency | quote }}
{{- if .Values.usage.priceCatalog }}
- name: LAZYCLOUD_USAGE_PRICE_CATALOG
  value: {{ .Values.usage.priceCatalog | quote }}
{{- end }}
- name: LAZYCLOUD_VOLUME_METERING_INTERVAL_SECONDS
  value: {{ .Values.usage.volumeMeteringIntervalSeconds | quote }}
- name: LAZYCLOUD_USAGE_METRICS_COLLECTOR
  value: {{ .Values.usage.metrics.collector | quote }}
- name: LAZYCLOUD_USAGE_METRICS_SOURCE
  value: {{ .Values.usage.metrics.source | quote }}
- name: LAZYCLOUD_USAGE_METRICS_PROMETHEUS_PORT
  value: {{ .Values.usage.metrics.prometheusPort | quote }}
{{- if .Values.usage.metrics.openmeterUrl }}
- name: LAZYCLOUD_USAGE_METRICS_OPENMETER_URL
  value: {{ .Values.usage.metrics.openmeterUrl | quote }}
{{- end }}
- name: LAZYCLOUD_MANAGED_BILLING_MODE
  value: {{ .Values.usage.managedBilling.mode | quote }}
{{- if .Values.usage.managedBilling.endpoint }}
- name: LAZYCLOUD_MANAGED_BILLING_ENDPOINT
  value: {{ .Values.usage.managedBilling.endpoint | quote }}
{{- end }}
- name: LAZYCLOUD_MANAGED_BILLING_TIMEOUT_SECONDS
  value: {{ .Values.usage.managedBilling.timeoutSeconds | quote }}
- name: LAZYCLOUD_MANAGED_BILLING_MINIMUM_CREDIT_CENTS
  value: {{ .Values.usage.managedBilling.minimumCreditCents | quote }}
- name: LAZYCLOUD_MANAGED_BILLING_REQUIRED
  value: {{ .Values.usage.managedBilling.required | quote }}
- name: LAZYCLOUD_TAILNET_ENABLED
  value: {{ .Values.tailnet.enabled | quote }}
{{- if .Values.tailnet.controlUrl }}
- name: LAZYCLOUD_TAILNET_CONTROL_URL
  value: {{ .Values.tailnet.controlUrl | quote }}
{{- end }}
{{- if .Values.tailnet.oauthClientId }}
- name: LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID
  value: {{ .Values.tailnet.oauthClientId | quote }}
{{- end }}
- name: LAZYCLOUD_TAILNET_AGENT_TAG
  value: {{ .Values.tailnet.agentTag | quote }}
- name: LAZYCLOUD_TAILNET_CONTROL_PLANE_TAG
  value: {{ .Values.tailnet.controlPlaneTag | quote }}
- name: LAZYCLOUD_TAILNET_API_URL
  value: {{ .Values.tailnet.apiUrl | quote }}
- name: LAZYCLOUD_TAILNET_AUTH_KEY_TTL_SECONDS
  value: {{ .Values.tailnet.authKeyTtlSeconds | quote }}
- name: LAZYCLOUD_TAILNET_MODE
  value: "sidecar"
- name: LAZYCLOUD_TAILNET_HOSTNAME
  value: {{ .Values.tailnet.hostname | quote }}
- name: LAZYCLOUD_TAILNET_STATE_DIR
  value: {{ .Values.tailnet.stateDir | quote }}
- name: LAZYCLOUD_TAILNET_SOCKET_PATH
  value: {{ .Values.tailnet.socketPath | quote }}
- name: LAZYCLOUD_TAILNET_ACCEPT_DNS
  value: {{ .Values.tailnet.acceptDns | quote }}
- name: LAZYCLOUD_TAILNET_ACCEPT_ROUTES
  value: {{ .Values.tailnet.acceptRoutes | quote }}
- name: LAZYCLOUD_TAILNET_USERSPACE_NETWORKING
  value: {{ .Values.tailnet.userspaceNetworking | quote }}
- name: LAZYCLOUD_TAILNET_LOGIN_TIMEOUT_SECONDS
  value: {{ .Values.tailnet.loginTimeoutSeconds | quote }}
- name: LAZYCLOUD_TAILNET_STATUS_TIMEOUT_SECONDS
  value: {{ .Values.tailnet.statusTimeoutSeconds | quote }}
- name: LAZYCLOUD_TAILNET_WAIT_POLL_SECONDS
  value: {{ .Values.tailnet.waitPollSeconds | quote }}
{{- end -}}

{{- define "lazycloud.schedulerEnv" -}}
- name: LAZYCLOUD_LOG_LEVEL
  value: {{ .Values.platform.logLevel | quote }}
- name: LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL
  value: {{ required "controlPlane.publicHttpUrl is required" .Values.controlPlane.publicHttpUrl | quote }}
- name: LAZYCLOUD_GATEWAY_RUNTIME_HTTP_URL
  value: {{ .Values.controlPlane.runtimeHttpUrl | default (printf "http://%s:%v" (include "lazycloud.fullname" .) .Values.service.port) | quote }}
- name: LAZYCLOUD_WORKSPACE_CHANGE_STREAM_MAX_LENGTH
  value: {{ .Values.events.workspaceChangeStreamMaxLength | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_ENABLED
  value: {{ .Values.awsCapacity.enabled | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_DRAFT_TTL_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.draftTtlSeconds | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CLEANUP_TOMBSTONE_TTL_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.cleanupTombstoneTtlSeconds | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CLEANUP_TIMEOUT_SECONDS
  value: {{ .Values.awsCapacity.lifecycle.cleanupTimeoutSeconds | quote }}
{{- if .Values.agentArtifact.version }}
- name: LAZYCLOUD_AGENT_BINARY_DIR
  value: {{ .Values.agentArtifact.volume.mountPath | required "agentArtifact.volume.mountPath is required" | quote }}
- name: LAZYCLOUD_AGENT_BINARY_VERSION
  value: {{ .Values.agentArtifact.version | quote }}
- name: LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH
  value: {{ dict "amd64" .Values.agentArtifact.sha256ByArch.amd64 | toJson | quote }}
{{- end }}
{{- if .Values.awsCapacity.enabled }}
- name: LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL
  value: {{ .Values.awsCapacity.connectionTemplateUrl | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST
  value: {{ .Values.awsCapacity.workerImageDigest | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS
  value: {{ .Values.awsCapacity.cpuAmiIds | toJson | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS
  value: {{ .Values.awsCapacity.gpuAmiIds | toJson | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN
  value: {{ .Values.awsCapacity.controlPrincipalArn | quote }}
- name: LAZYCLOUD_AWS_CAPACITY_INSTANCE_HOURLY_MICROS
  value: {{ .Values.awsCapacity.instanceHourlyMicros | toJson | quote }}
- name: LAZYCLOUD_AWS_CONNECTION_EXTERNAL_ID_BYTES
  value: {{ .Values.awsCapacity.externalIdBytes | quote }}
{{- end }}
{{- if .Values.imageBuild.container.poolSelector }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_POOL_SELECTOR
  value: {{ .Values.imageBuild.container.poolSelector | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_CPU_MILLICORES
  value: {{ .Values.imageBuild.container.cpuMillicores | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_MEMORY_MIB
  value: {{ .Values.imageBuild.container.memoryMib | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_TIMEOUT_SECONDS
  value: {{ .Values.imageBuild.container.addressWaitTimeoutSeconds | quote }}
- name: LAZYCLOUD_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_INTERVAL_SECONDS
  value: {{ .Values.imageBuild.container.addressPollIntervalSeconds | quote }}
{{- if .Values.imageBuild.archive.bucket }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BUCKET
  value: {{ .Values.imageBuild.archive.bucket | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.presignedEndpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PRESIGNED_ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.presignedEndpointUrl | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.prefix }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PREFIX
  value: {{ .Values.imageBuild.archive.prefix | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_PRESIGN_SECONDS
  value: {{ .Values.imageBuild.archive.presignSeconds | quote }}
{{- if .Values.imageBuild.archive.backend.enabled }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__BUCKET
  value: {{ include "lazycloud.imageArchiveBucket" . | quote }}
{{- if .Values.imageBuild.archive.backend.endpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.backend.endpointUrl | quote }}
{{- end }}
{{- if .Values.imageBuild.archive.backend.presignedEndpointUrl }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__PRESIGNED_ENDPOINT_URL
  value: {{ .Values.imageBuild.archive.backend.presignedEndpointUrl | quote }}
{{- end }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__REGION_NAME
  value: {{ .Values.imageBuild.archive.backend.regionName | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__FORCE_PATH_STYLE
  value: {{ .Values.imageBuild.archive.backend.forcePathStyle | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MULTIPART_THRESHOLD_BYTES
  value: {{ .Values.imageBuild.archive.backend.transferMultipartThresholdBytes | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MULTIPART_CHUNK_SIZE_BYTES
  value: {{ .Values.imageBuild.archive.backend.transferMultipartChunkSizeBytes | quote }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__TRANSFER_MAX_CONCURRENCY
  value: {{ .Values.imageBuild.archive.backend.transferMaxConcurrency | quote }}
{{- end }}
- name: LAZYCLOUD_RETENTION_ENABLED
  value: {{ .Values.scheduler.retention.enabled | quote }}
- name: LAZYCLOUD_RETENTION_INTERVAL_SECONDS
  value: {{ .Values.scheduler.retention.intervalSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RETRY_INITIAL_SECONDS
  value: {{ .Values.scheduler.retention.retryInitialSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RETRY_MAX_SECONDS
  value: {{ .Values.scheduler.retention.retryMaxSeconds | quote }}
- name: LAZYCLOUD_RETENTION_RECENT_STUB_TTL_SECONDS
  value: {{ .Values.scheduler.retention.recentStubTtlSeconds | quote }}
- name: LAZYCLOUD_RETENTION_SOURCE_GRACE_SECONDS
  value: {{ .Values.scheduler.retention.sourceGraceSeconds | quote }}
- name: LAZYCLOUD_RETENTION_CHECKPOINT_SECONDS
  value: {{ .Values.scheduler.retention.checkpointSeconds | quote }}
- name: LAZYCLOUD_RETENTION_BUILD_SECONDS
  value: {{ .Values.scheduler.retention.buildSeconds | quote }}
- name: LAZYCLOUD_RETENTION_IMAGE_SECONDS
  value: {{ .Values.scheduler.retention.imageSeconds | quote }}
- name: LAZYCLOUD_RETENTION_MAX_ITEMS_PER_CYCLE
  value: {{ .Values.scheduler.retention.maxItemsPerCycle | quote }}
- name: LAZYCLOUD_USAGE_BILLING_CURRENCY
  value: {{ .Values.usage.billingCurrency | quote }}
{{- if .Values.usage.priceCatalog }}
- name: LAZYCLOUD_USAGE_PRICE_CATALOG
  value: {{ .Values.usage.priceCatalog | quote }}
{{- end }}
- name: LAZYCLOUD_VOLUME_METERING_INTERVAL_SECONDS
  value: {{ .Values.usage.volumeMeteringIntervalSeconds | quote }}
- name: LAZYCLOUD_USAGE_METRICS_COLLECTOR
  value: {{ .Values.usage.metrics.collector | quote }}
- name: LAZYCLOUD_USAGE_METRICS_SOURCE
  value: {{ .Values.usage.metrics.source | quote }}
- name: LAZYCLOUD_USAGE_METRICS_PROMETHEUS_PORT
  value: {{ .Values.usage.metrics.prometheusPort | quote }}
{{- if .Values.usage.metrics.openmeterUrl }}
- name: LAZYCLOUD_USAGE_METRICS_OPENMETER_URL
  value: {{ .Values.usage.metrics.openmeterUrl | quote }}
{{- end }}
- name: LAZYCLOUD_MANAGED_BILLING_MODE
  value: {{ .Values.usage.managedBilling.mode | quote }}
{{- if .Values.usage.managedBilling.endpoint }}
- name: LAZYCLOUD_MANAGED_BILLING_ENDPOINT
  value: {{ .Values.usage.managedBilling.endpoint | quote }}
{{- end }}
- name: LAZYCLOUD_MANAGED_BILLING_TIMEOUT_SECONDS
  value: {{ .Values.usage.managedBilling.timeoutSeconds | quote }}
- name: LAZYCLOUD_MANAGED_BILLING_MINIMUM_CREDIT_CENTS
  value: {{ .Values.usage.managedBilling.minimumCreditCents | quote }}
- name: LAZYCLOUD_MANAGED_BILLING_REQUIRED
  value: {{ .Values.usage.managedBilling.required | quote }}
- name: LAZYCLOUD_TAILNET_ENABLED
  value: {{ .Values.tailnet.enabled | quote }}
{{- if .Values.tailnet.controlUrl }}
- name: LAZYCLOUD_TAILNET_CONTROL_URL
  value: {{ .Values.tailnet.controlUrl | quote }}
{{- end }}
{{- if .Values.tailnet.oauthClientId }}
- name: LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID
  value: {{ .Values.tailnet.oauthClientId | quote }}
{{- end }}
- name: LAZYCLOUD_TAILNET_AGENT_TAG
  value: {{ .Values.tailnet.agentTag | quote }}
- name: LAZYCLOUD_TAILNET_CONTROL_PLANE_TAG
  value: {{ .Values.tailnet.controlPlaneTag | quote }}
- name: LAZYCLOUD_TAILNET_API_URL
  value: {{ .Values.tailnet.apiUrl | quote }}
- name: LAZYCLOUD_TAILNET_AUTH_KEY_TTL_SECONDS
  value: {{ .Values.tailnet.authKeyTtlSeconds | quote }}
- name: LAZYCLOUD_TAILNET_MODE
  value: "sidecar"
- name: LAZYCLOUD_TAILNET_HOSTNAME
  value: {{ .Values.tailnet.hostname | quote }}
- name: LAZYCLOUD_TAILNET_STATE_DIR
  value: {{ .Values.tailnet.stateDir | quote }}
- name: LAZYCLOUD_TAILNET_SOCKET_PATH
  value: {{ .Values.tailnet.socketPath | quote }}
- name: LAZYCLOUD_TAILNET_ACCEPT_DNS
  value: {{ .Values.tailnet.acceptDns | quote }}
- name: LAZYCLOUD_TAILNET_ACCEPT_ROUTES
  value: {{ .Values.tailnet.acceptRoutes | quote }}
- name: LAZYCLOUD_TAILNET_USERSPACE_NETWORKING
  value: {{ .Values.tailnet.userspaceNetworking | quote }}
- name: LAZYCLOUD_TAILNET_LOGIN_TIMEOUT_SECONDS
  value: {{ .Values.tailnet.loginTimeoutSeconds | quote }}
- name: LAZYCLOUD_TAILNET_STATUS_TIMEOUT_SECONDS
  value: {{ .Values.tailnet.statusTimeoutSeconds | quote }}
- name: LAZYCLOUD_TAILNET_WAIT_POLL_SECONDS
  value: {{ .Values.tailnet.waitPollSeconds | quote }}
{{- if .Values.containerWorkers.enabled }}
- name: LAZYCLOUD_KUBERNETES_CAPACITY_POOLS
  value: {{ include "lazycloud.kubernetesCapacityPools" . | quote }}
{{- if eq (include "lazycloud.hasKubernetesCapacityScaling" .) "true" }}
- name: LAZYCLOUD_WORKER_POOL_SCALER_KUBERNETES_NAME
  value: {{ include "lazycloud.fullname" . | quote }}
- name: LAZYCLOUD_WORKER_POOL_SCALER_KUBERNETES_NAMESPACE
  value: {{ .Release.Namespace | quote }}
{{- end }}
{{- end }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_STALE_GRACE_SECONDS
  value: {{ .Values.scheduler.computeReclaim.staleGraceSeconds | quote }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_BOOTSTRAP_PHASE_DEADLINE_SECONDS
  value: {{ .Values.scheduler.computeReclaim.bootstrapPhaseDeadlineSeconds | toJson | quote }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_MAX_LAUNCH_ATTEMPTS
  value: {{ .Values.scheduler.computeReclaim.maxLaunchAttempts | quote }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_PROVIDER_STALE_GRACE_SECONDS
  value: {{ .Values.scheduler.computeReclaim.providerStaleGraceSeconds | toJson | quote }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_PROVIDER_BOOTSTRAP_PHASE_DEADLINE_SECONDS
  value: {{ .Values.scheduler.computeReclaim.providerBootstrapPhaseDeadlineSeconds | toJson | quote }}
- name: LAZYCLOUD_COMPUTE_RECLAIM_PROVIDER_MAX_LAUNCH_ATTEMPTS
  value: {{ .Values.scheduler.computeReclaim.providerMaxLaunchAttempts | toJson | quote }}
{{- end -}}

{{- define "lazycloud.workerEnv" -}}
- name: LAZYCLOUD_LOG_LEVEL
  value: {{ .Values.platform.logLevel | quote }}
- name: LAZYCLOUD_WORKSPACE_CHANGE_STREAM_MAX_LENGTH
  value: {{ .Values.events.workspaceChangeStreamMaxLength | quote }}
{{- end -}}

{{- define "lazycloud.integrationSecretEnv" -}}
- name: LAZYCLOUD_MANAGED_BILLING_AUTH_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.usage.managedBilling.authTokenSecretRef.name | quote }}
      key: {{ .Values.usage.managedBilling.authTokenSecretRef.key | quote }}
      optional: true
- name: LAZYCLOUD_MANAGED_BILLING_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.usage.managedBilling.headersSecretRef.name | quote }}
      key: {{ .Values.usage.managedBilling.headersSecretRef.key | quote }}
      optional: true
- name: LAZYCLOUD_USAGE_METRICS_OPENMETER_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.usage.metrics.openmeterApiKeySecretRef.name | quote }}
      key: {{ .Values.usage.metrics.openmeterApiKeySecretRef.key | quote }}
      optional: true
- name: LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.tailnet.oauthClientSecretRef.name | quote }}
      key: {{ .Values.tailnet.oauthClientSecretRef.key | quote }}
      optional: {{ not .Values.tailnet.enabled }}
{{- end -}}

{{- define "lazycloud.routeSecretEnv" -}}
- name: LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.tailnet.routeAuthKeySecretRef.name | quote }}
      key: {{ .Values.tailnet.routeAuthKeySecretRef.key | quote }}
      optional: {{ not .Values.tailnet.enabled }}
{{- end -}}

{{- define "lazycloud.apiSecretEnv" -}}
{{ include "lazycloud.integrationSecretEnv" . }}
{{ include "lazycloud.routeSecretEnv" . }}
{{ include "lazycloud.imageArchiveSecretEnv" . }}
{{ include "lazycloud.containerServiceSecretEnv" . }}
{{ include "lazycloud.originRegistrySecretEnv" . }}
{{- end -}}

{{- define "lazycloud.originRegistrySecretEnv" -}}
{{- if .Values.imageBuild.originRegistry.credentialsSecretRef.name }}
- name: LAZYCLOUD_BUILD_REGISTRY_CREDENTIALS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.imageBuild.originRegistry.credentialsSecretRef.name | quote }}
      key: {{ .Values.imageBuild.originRegistry.credentialsSecretRef.key | quote }}
      optional: false
{{- end }}
{{- end -}}

{{- define "lazycloud.imageArchiveSecretEnv" -}}
{{- if and .Values.imageBuild.archive.backend.enabled .Values.imageBuild.archive.backend.credentialsSecretRef.name }}
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ .Values.imageBuild.archive.backend.credentialsSecretRef.name | quote }}
      key: {{ .Values.imageBuild.archive.backend.credentialsSecretRef.accessKeyIdKey | quote }}
      optional: false
- name: LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.imageBuild.archive.backend.credentialsSecretRef.name | quote }}
      key: {{ .Values.imageBuild.archive.backend.credentialsSecretRef.secretAccessKeyKey | quote }}
      optional: false
{{- end }}
{{- end -}}

{{- define "lazycloud.containerServiceSecretEnv" -}}
- name: LAZYCLOUD_CONTAINER_SERVICE_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ default (include "lazycloud.configSecretName" .) .Values.containerService.tokenSecretRef.name | quote }}
      key: {{ .Values.containerService.tokenSecretRef.key | quote }}
      optional: true
{{- end -}}

{{- define "lazycloud.objectStoreEndpoint" -}}
{{- if .Values.objectStore.endpointUrl -}}
{{- .Values.objectStore.endpointUrl -}}
{{- else if .Values.objectStore.enabled -}}
{{- printf "http://%s:9000" (include "lazycloud.objectStoreName" .) -}}
{{- else -}}
{{- required "objectStore.endpointUrl is required when bundled object storage is disabled" .Values.objectStore.endpointUrl -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.databaseWaitInitContainer" -}}
- name: database-ready
  image: "{{ .Values.databaseBootstrap.image.repository }}:{{ .Values.databaseBootstrap.image.tag }}"
  imagePullPolicy: {{ .Values.databaseBootstrap.image.pullPolicy | quote }}
  env:
    {{- include "lazycloud.databaseEnv" . | nindent 4 }}
  command:
    - lazycloud-admin
    - --json
    - database
    - wait
    - --timeout-seconds
    - {{ .Values.databaseBootstrap.readiness.timeoutSeconds | quote }}
    - --poll-interval-seconds
    - {{ .Values.databaseBootstrap.readiness.pollIntervalSeconds | quote }}
  resources:
    {{- toYaml .Values.databaseBootstrap.readiness.resources | nindent 4 }}
{{- end -}}

{{- define "lazycloud.administratorWaitInitContainer" -}}
- name: administrator-ready
  image: "{{ .Values.databaseBootstrap.image.repository }}:{{ .Values.databaseBootstrap.image.tag }}"
  imagePullPolicy: {{ .Values.databaseBootstrap.image.pullPolicy | quote }}
  env:
    {{- include "lazycloud.databaseEnv" . | nindent 4 }}
    - name: LAZYCLOUD_ADMINISTRATOR_READY_TIMEOUT_SECONDS
      value: {{ .Values.administratorBootstrap.readiness.timeoutSeconds | quote }}
    - name: LAZYCLOUD_ADMINISTRATOR_READY_POLL_SECONDS
      value: {{ .Values.administratorBootstrap.readiness.pollIntervalSeconds | quote }}
  command:
    - python
    - -c
  args:
    - |
      import os
      import time

      from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
      from identity.auth import AuthService, IdentityDatabaseContext

      timeout = float(os.environ["LAZYCLOUD_ADMINISTRATOR_READY_TIMEOUT_SECONDS"])
      poll = float(os.environ["LAZYCLOUD_ADMINISTRATOR_READY_POLL_SECONDS"])
      deadline = time.monotonic() + timeout
      database = DatabaseClient.from_settings(
          DatabaseSettings(application_name=DatabaseApplicationName.Wait)
      )
      try:
          service = AuthService(IdentityDatabaseContext(database))
          while not service.administrator_ready():
              if time.monotonic() >= deadline:
                  raise TimeoutError("administrator bootstrap did not become ready")
              time.sleep(poll)
      finally:
          database.dispose()
  resources:
    {{- toYaml .Values.administratorBootstrap.readiness.resources | nindent 4 }}
{{- end -}}

{{- define "lazycloud.workerTokenWaitInitContainer" -}}
- name: worker-token-ready
  image: "{{ .root.Values.databaseBootstrap.image.repository }}:{{ .root.Values.databaseBootstrap.image.tag }}"
  imagePullPolicy: {{ .root.Values.databaseBootstrap.image.pullPolicy | quote }}
  env:
    - name: WORKER_TOKEN
      valueFrom:
        secretKeyRef:
          name: {{ .workerTokenSecretName | quote }}
          key: {{ .workerTokenSecretKey | quote }}
          optional: false
  command:
    - /bin/sh
    - -c
    - test -n "$WORKER_TOKEN"
  resources:
    {{- toYaml .root.Values.databaseBootstrap.readiness.resources | nindent 4 }}
{{- end -}}

{{- define "lazycloud.hpaMetrics" -}}
{{- with .targetCPUUtilizationPercentage }}
metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: {{ . }}
{{- with $.targetMemoryUtilizationPercentage }}
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: {{ . }}
{{- end }}
{{- else }}
{{- with .targetMemoryUtilizationPercentage }}
metrics:
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: {{ . }}
{{- end }}
{{- end }}
{{- end -}}

{{- define "lazycloud.pdbAvailability" -}}
{{- with .minAvailable }}
minAvailable: {{ . }}
{{- else }}
{{- with .maxUnavailable }}
maxUnavailable: {{ . }}
{{- end }}
{{- end }}
{{- end -}}
