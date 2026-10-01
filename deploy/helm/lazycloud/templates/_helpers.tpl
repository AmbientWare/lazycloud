{{/* The image reference of a component: (list $ "server"). */}}
{{- define "lazycloud.image" -}}
{{- $root := index . 0 -}}
{{- $component := index $root.Values (index . 1) -}}
{{- $registry := required "image.registry is required" $root.Values.image.registry -}}
{{- if $component.digest -}}
{{- printf "%s/%s@%s" $registry $component.repository $component.digest -}}
{{- else -}}
{{- printf "%s/%s:%s" $registry $component.repository (required "image.tag or a component digest is required" $root.Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/* Settings the chart owns; values may not set them. */}}
{{- define "lazycloud.ownedKeys" -}}
{{- list "LAZYCLOUD_HTTP_ADDR" "LAZYCLOUD_GRPC_ADDR" "LAZYCLOUD_HEALTH_ADDR" "LAZYCLOUD_METRICS_ADDR" "LAZYCLOUD_SECRETS_KEY_FILE" "LAZYCLOUD_DRAIN_DELAY" "LAZYCLOUD_EDGE_ADDR" "LAZYCLOUD_EDGE_RELAY_ADDR" "LAZYCLOUD_EDGE_RELAY_ADVERTISE" | toJson -}}
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
app.kubernetes.io/version: {{ (index . 0).Values.image.tag | default "digest" | quote }}
app.kubernetes.io/managed-by: {{ (index . 0).Release.Service }}
{{- end -}}
