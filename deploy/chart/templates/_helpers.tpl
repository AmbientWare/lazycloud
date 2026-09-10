{{- define "lazycloud.env" -}}
{{- $root := index . 0 -}}
{{- $consumer := index . 1 -}}
{{- $binding := index $root.Values.environment $consumer -}}
{{- range $variable := $binding.secretKeys }}
{{- if not (hasKey $root.Values.secrets.map $variable) -}}
{{- fail (printf "%s requires an unbound secret key %s" $consumer $variable) -}}
{{- end }}
{{- if hasKey $root.Values.runtime $variable -}}
{{- fail (printf "%s must not appear in plaintext runtime values" $variable) -}}
{{- end }}
- name: {{ $variable }}
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.secrets.name }}
      key: {{ $variable }}
{{- end }}
{{- range $variable := $binding.runtimeKeys }}
{{- if not (hasKey $root.Values.runtime $variable) -}}
{{- fail (printf "%s requires runtime value %s" $consumer $variable) -}}
{{- end }}
- name: {{ $variable }}
  value: {{ index $root.Values.runtime $variable | quote }}
{{- end }}
{{- end -}}

{{- define "lazycloud.image" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- if eq $name "tunnel-gateway" -}}
{{- $digest := required "image.networkDigest must name the executable network image manifest" $root.Values.image.networkDigest -}}
{{- printf "%s/%s/%s@%s" $root.Values.image.registry $root.Values.image.repositoryPrefix $name $digest -}}
{{- else -}}
{{- $tag := $root.Values.image.tag -}}
{{- if not $tag -}}
{{- fail "image.tag was not supplied; CI writes it to the deployment branch after it builds" -}}
{{- end -}}
{{- printf "%s/%s/%s:%s" $root.Values.image.registry $root.Values.image.repositoryPrefix $name $tag -}}
{{- end -}}
{{- end -}}

{{/* The chart writes this profile and names it for every consumer. */}}
{{- define "lazycloud.awsProfile" -}}control{{- end -}}

{{/*
Count the whole workload across revisions so a rollout retains fault isolation.
minDomains keeps the second replica Pending when only one domain exists, giving
Auto Mode a reason to provision capacity in another node and zone.
*/}}
{{- define "lazycloud.spreadAcrossNodes" -}}
{{- range $topology := list "kubernetes.io/hostname" "topology.kubernetes.io/zone" }}
- maxSkew: 1
  minDomains: 2
  topologyKey: {{ $topology }}
  whenUnsatisfiable: DoNotSchedule
  labelSelector:
    matchLabels:
      app: {{ $ }}
{{- end }}
{{- end -}}

{{- define "lazycloud.databaseEnv" -}}
- name: LAZYCLOUD_DATABASE_POOL_SIZE
  value: {{ .poolSize | quote }}
- name: LAZYCLOUD_DATABASE_MAX_OVERFLOW
  value: {{ .maxOverflow | quote }}
{{- end -}}

{{/* Transaction clients share the Terraform-bounded pooler. Session locks are direct. */}}
{{- define "lazycloud.databaseBudget" -}}
{{- if le (int .Values.scheduler.minReadySeconds) (int .Values.scheduler.terminationGracePeriodSeconds) -}}
{{- fail "scheduler.minReadySeconds must exceed terminationGracePeriodSeconds to bound rollout overlap" -}}
{{- end -}}
{{- $ceiling := int .Values.database.maxConnections -}}
{{- $reserved := int .Values.database.reserved -}}
{{- $pooler := int .Values.database.poolerMaxConnections -}}
{{- $direct := mul 2 (int .Values.controlPlane.replicas) -}}
{{- $jobs := add (int .Values.bootstrap.database.poolSize) (int .Values.bootstrap.database.maxOverflow) -}}
{{- $total := add $pooler $direct $jobs $reserved -}}
{{- if gt $total $ceiling -}}
{{- fail (printf "database budget %d exceeds server ceiling %d (pooler %d, session locks %d, jobs %d, reserve %d)" $total $ceiling $pooler $direct $jobs $reserved) -}}
{{- end -}}
{{- end -}}
