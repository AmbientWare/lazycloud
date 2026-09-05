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

{{/*
One image reference, built from the registry, the repository and the tag.

Refuses to render when the tag is absent. Helm's default for a missing value is
the empty string, which produces a Deployment whose image ends in `:` and fails
at the pod with `InvalidImageName` -- several steps from the values file that
forgot it, and reported as though the image were wrong rather than missing.
*/}}
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

{{/*
The SDK profile the chain is written under and the workloads read.

Both ends are in this chart, so it is a name rather than a setting: nothing
outside picks it, and a value would be a way for the file and the process that
reads it to disagree.
*/}}
{{- define "lazycloud.awsProfile" -}}control{{- end -}}

{{/*
One replica per node, for the workloads where a second replica is redundancy.

A name rather than a setting, like the profile above: nothing outside this chart
picks it, and both workloads that take it want it for the same reason. Two
replicas of the API on one node are one replica that costs twice as much, and
two connectors on one node are one connector -- the node is the failure they
were both added to survive.

`DoNotSchedule` because the alternative does nothing here. `ScheduleAnyway` is a
preference, and a preference is satisfied by the packing that already exists; a
requirement leaves the second replica Pending, and Pending is the only state
Karpenter provisions for. So this is also what asks for a second node. A cluster
sized purely by whether the pods fit will always answer that they do, on one.

`matchLabelKeys` scopes the skew to one ReplicaSet. Without it a rolling update
counts the pods it is replacing, the surge pod satisfies no placement on either
node, and the rollout waits for a third node it does not need and gives back
minutes later.
*/}}
{{- define "lazycloud.spreadAcrossNodes" -}}
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: DoNotSchedule
  matchLabelKeys:
    - pod-template-hash
  labelSelector:
    matchLabels:
      app: {{ . }}
{{- end -}}

{{/*
One workload's database pool, as environment.

Per workload rather than in the shared env block, because the processes differ:
the API serves concurrent requests, the scheduler runs a few loops, and a
bootstrap job is one thread that exits. A single value for all of them is either
too small for the API or, multiplied across every pod, larger than the server
allows.
*/}}
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
