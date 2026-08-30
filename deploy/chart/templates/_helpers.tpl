{{/*
The env block every workload shares.

Derived from the secret map, because a list of names beside a map of the same
names is two statements of which variables exist and they drift apart silently.
The Secret holds what the map names; a pod asking for a key the Secret lacks
does not start, and nothing reports which of the two was wrong.
*/}}
{{- define "lazycloud.env" -}}
{{- range $variable, $secret := $.Values.secrets.map }}
- name: {{ $variable }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ $variable }}
{{- end }}
{{- include "lazycloud.runtimeEnv" . }}
{{- end -}}

{{- define "lazycloud.runtimeEnv" -}}
{{- range $key, $value := $.Values.runtime }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{- define "lazycloud.pangolinControlEnv" -}}
{{- range $variable, $_ := $.Values.secrets.pangolinControl.map }}
- name: {{ $variable }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.pangolinControl.name }}
      key: {{ $variable }}
{{- end }}
{{- end -}}

{{- define "lazycloud.pangolinRuntimeProviderEnv" -}}
- name: LAZYCLOUD_PANGOLIN_PLATFORM_SITE_IDS
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.pangolinRuntime.provider.name }}
      key: LAZYCLOUD_PANGOLIN_PLATFORM_SITE_IDS
- name: LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_RECORD_IDS
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.pangolinRuntime.provider.name }}
      key: LAZYCLOUD_PANGOLIN_PLATFORM_CLIENT_RECORD_IDS
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
{{- $tag := $root.Values.image.tag -}}
{{- if not $tag -}}
{{- fail "image.tag was not supplied; CI writes it to the deployment branch after it builds" -}}
{{- end -}}
{{- printf "%s/%s/%s:%s" $root.Values.image.registry $root.Values.image.repositoryPrefix $name $tag -}}
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
Spread separately declared ordinal Deployments as one replicated workload.

These pods do not share a ReplicaSet because each projects a different
credential. Selecting the common app label keeps the placement decision across
the full set. A rollout may add one surge pod without asking for a third node:
with the old replicas balanced, placing the surge beside either one produces a
skew of one, then returns to zero when the old pod exits.
*/}}
{{- define "lazycloud.spreadOrdinalSetAcrossNodes" -}}
- maxSkew: 1
  topologyKey: kubernetes.io/hostname
  whenUnsatisfiable: DoNotSchedule
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

{{/*
Refuse to render a deployment that cannot connect.

The ceiling is shared and nothing enforces it: exceeding it produces connection
timeouts under load, in whichever process happens to ask last, minutes after the
value that caused it was changed. Checked here so it is a rendering error naming
the sum instead.

One job's worth, not every job's: the three that open a database are each in a
sync wave of their own and the fourth opens none, so they never hold connections
at once. The worst moment is a job running while the previous release's pods
still serve, which this counts.
*/}}
{{- define "lazycloud.databaseBudget" -}}
{{- $ceiling := int .Values.database.maxConnections -}}
{{- $reserved := int .Values.database.reserved -}}
{{- $api := mul (int .Values.controlPlane.replicas) (add (int .Values.controlPlane.database.poolSize) (int .Values.controlPlane.database.maxOverflow)) -}}
{{- $scheduler := mul (int .Values.scheduler.replicas) (add (int .Values.scheduler.database.poolSize) (int .Values.scheduler.database.maxOverflow)) -}}
{{- $jobs := add (int .Values.bootstrap.database.poolSize) (int .Values.bootstrap.database.maxOverflow) -}}
{{- $total := add $api $scheduler $jobs -}}
{{- if gt (add $total $reserved) $ceiling -}}
{{- fail (printf "database pools may open %d connections (control plane %d, scheduler %d, jobs %d) with %d reserved, and the server allows %d" $total $api $scheduler $jobs $reserved $ceiling) -}}
{{- end -}}
{{- end -}}
