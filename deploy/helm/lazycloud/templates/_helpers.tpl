{{/* The image reference of a component: (list $ "server"). */}}
{{- define "lazycloud.image" -}}
{{- $root := index . 0 -}}
{{- $component := index $root.Values (index . 1) -}}
{{- $registry := required "image.registry is required" $root.Values.image.registry -}}
{{- if $component.digest -}}
{{- printf "%s/%s@%s" $registry $component.repository $component.digest -}}
{{- else -}}
{{- printf "%s/%s:%s" $registry $component.repository (include "lazycloud.version" $root) -}}
{{- end -}}
{{- end -}}

{{- define "lazycloud.version" -}}
{{- $version := required "release.version is required" .Values.release.version -}}
{{- if not (regexMatch "^[A-Za-z0-9][A-Za-z0-9._+-]*$" $version) -}}
{{- fail (printf "release.version %q is not a release name" $version) -}}
{{- end -}}
{{- $version -}}
{{- end -}}

{{- define "lazycloud.domain" -}}
{{- required "domain is required" .Values.domain -}}
{{- end -}}

{{/* Settings the chart owns; values may not set them. */}}
{{- define "lazycloud.ownedKeys" -}}
{{- list "LAZYCLOUD_HTTP_ADDR" "LAZYCLOUD_GRPC_ADDR" "LAZYCLOUD_GRPC_TLS_CERT" "LAZYCLOUD_GRPC_TLS_KEY" "LAZYCLOUD_HEALTH_ADDR" "LAZYCLOUD_METRICS_ADDR" "LAZYCLOUD_SECRETS_KEY_FILE" "LAZYCLOUD_DRAIN_DELAY" "LAZYCLOUD_EDGE_ADDR" "LAZYCLOUD_EDGE_RELAY_ADDR" "LAZYCLOUD_EDGE_RELAY_ADVERTISE" "LAZYCLOUD_EDGE_TCP_ADDR" "LAZYCLOUD_EDGE_TCP_URL" "LAZYCLOUD_EDGE_TCP_CERT" "LAZYCLOUD_EDGE_TCP_KEY" "LAZYCLOUD_EDGE_URL" "LAZYCLOUD_PUBLIC_URL" "LAZYCLOUD_INSTALL_URL" "LAZYCLOUD_AGENT_SERVER_ADDR" "LAZYCLOUD_AGENT_DIST_DIR" "LAZYCLOUD_CLIENT_RELEASE_VERSION" "LAZYCLOUD_OTLP_ENDPOINT" "LAZYCLOUD_OTLP_INSECURE" "LAZYCLOUD_TRACE_SAMPLE_RATIO" | toJson -}}
{{- end -}}

{{/*
Environment of a component: shared and component settings as values, then
its secret keys as references. (list $ "server").
*/}}
{{- define "lazycloud.env" -}}
{{- $root := index . 0 -}}
{{- $name := index . 1 -}}
{{- $component := index $root.Values $name -}}
{{- $owned := include "lazycloud.ownedKeys" . | fromJsonArray -}}
{{- $config := merge (deepCopy ($component.config | default dict)) $root.Values.config -}}
{{- if and (hasKey $config "LAZYCLOUD_FLEET_NETWORKS") (not (hasKey $config "LAZYCLOUD_FLEET_IMAGES")) }}
{{- fail "LAZYCLOUD_FLEET_NETWORKS needs LAZYCLOUD_FLEET_IMAGES: without the baked node images platform hosts run workloads without gVisor" }}
{{- end }}
{{- range $key := keys $config | sortAlpha }}
{{- if has $key $owned }}
{{- fail (printf "%s is set by the chart, not by config" $key) }}
{{- end }}
{{- if has $key $component.secretKeys }}
{{- fail (printf "%s is a secret key and must not appear in config" $key) }}
{{- end }}
- name: {{ $key }}
  value: {{ index $config $key | toString | quote }}
{{- end }}
{{- range $key := $component.secretKeys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ $root.Values.secrets.name }}
      key: {{ $key }}
{{- end }}
{{- end -}}

{{/* Where hosts dial and download the agent; the server and scheduler both hand them out. */}}
{{- define "lazycloud.hostEnv" -}}
- name: LAZYCLOUD_AGENT_SERVER_ADDR
  value: {{ printf "hosts.%s:443" (include "lazycloud.domain" .) | quote }}
- name: LAZYCLOUD_INSTALL_URL
  value: {{ printf "https://%s" (include "lazycloud.domain" .) | quote }}
{{- end -}}

{{/* The server and scheduler reach AWS through Pod Identity in this region. */}}
{{- define "lazycloud.awsEnv" -}}
{{- with .Values.awsRegion }}
- name: AWS_REGION
  value: {{ . | quote }}
{{- end }}
{{- end -}}

{{/* Traces go to the in-namespace collector when telemetry is on. */}}
{{- define "lazycloud.telemetryEnv" -}}
- name: LAZYCLOUD_OTLP_ENDPOINT
  value: "otel-collector:4317"
- name: LAZYCLOUD_OTLP_INSECURE
  value: "true"
- name: LAZYCLOUD_TRACE_SAMPLE_RATIO
  value: {{ .Values.telemetry.sampleRatio | quote }}
- name: LAZYCLOUD_EDGE_TRACE_SAMPLE_RATIO
  value: {{ .Values.telemetry.edgeSampleRatio | quote }}
{{- end -}}

{{/* The master key file, readable by the pod's fsGroup only. */}}
{{- define "lazycloud.masterKeyVolume" -}}
- name: master-key
  secret:
    secretName: {{ .Values.secrets.name }}
    defaultMode: 0440
    items:
      - key: {{ .Values.secrets.masterKey }}
        path: secrets.key
{{- end -}}

{{- define "lazycloud.masterKeyMount" -}}
- name: master-key
  mountPath: /run/lazycloud/secrets
  readOnly: true
{{- end -}}

{{/*
Replicas spread over nodes and zones, counting old and new revisions, so one
node or zone loss leaves a replica. minDomains keeps a second replica Pending
in a single-zone cluster, which makes the autoscaler add capacity elsewhere.
*/}}
{{- define "lazycloud.spread" -}}
{{- range $topology := list "kubernetes.io/hostname" "topology.kubernetes.io/zone" }}
- maxSkew: 1
  minDomains: 2
  topologyKey: {{ $topology }}
  whenUnsatisfiable: DoNotSchedule
  labelSelector:
    matchLabels:
      app.kubernetes.io/name: {{ $ }}
{{- end }}
{{- end -}}

{{- define "lazycloud.labels" -}}
app.kubernetes.io/name: {{ index . 1 }}
app.kubernetes.io/part-of: lazycloud
app.kubernetes.io/version: {{ (index . 0).Values.release.version | quote }}
app.kubernetes.io/managed-by: {{ (index . 0).Release.Service }}
{{- end -}}

{{/* NetworkPolicy ingress rules from CIDRs or from release pods to named ports. */}}
{{- define "lazycloud.cidrPeers" -}}
- from:
    {{- range .cidrs }}
    - ipBlock: { cidr: {{ . }} }
    {{- end }}
  ports:
    {{- range .ports }}
    - port: {{ . }}
    {{- end }}
{{- end -}}
{{- define "lazycloud.podPeers" -}}
- from:
    {{- range .names }}
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: {{ . }}
    {{- end }}
  ports:
    {{- range .ports }}
    - port: {{ . }}
    {{- end }}
{{- end -}}
