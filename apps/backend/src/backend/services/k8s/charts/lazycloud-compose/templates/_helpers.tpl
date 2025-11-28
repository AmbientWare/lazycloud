{{/*
Expand the name of the chart.
*/}}
{{- define "lazycloud-compose.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "lazycloud-compose.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "lazycloud-compose.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "lazycloud-compose.labels" -}}
helm.sh/chart: {{ include "lazycloud-compose.chart" . }}
{{ include "lazycloud-compose.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- if .Values.global.labels }}
{{ toYaml .Values.global.labels }}
{{- end }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Common annotations
*/}}
{{- define "lazycloud-compose.annotations" -}}
{{- if .Values.global.annotations }}
{{ toYaml .Values.global.annotations }}
{{- end }}
{{- with .Values.commonAnnotations }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "lazycloud-compose.selectorLabels" -}}
app.kubernetes.io/name: {{ include "lazycloud-compose.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service selector labels for a specific service
*/}}
{{- define "lazycloud-compose.serviceSelectorLabels" -}}
app.kubernetes.io/name: {{ .serviceName }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "lazycloud-compose.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "lazycloud-compose.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Get namespace for a service
All services use the namespace where Helm is deploying (Release.Namespace)
Since this is called with dict context, we need to access the root context
*/}}
{{- define "lazycloud-compose.serviceNamespace" -}}
{{- if .root -}}
{{- .root.Release.Namespace -}}
{{- else if .Release -}}
{{- .Release.Namespace -}}
{{- else -}}
{{- "default" -}}
{{- end -}}
{{- end }}

{{/*
Sanitize name to be DNS-1123 compliant
*/}}
{{- define "lazycloud-compose.sanitizeName" -}}
{{- . | lower | replace "_" "-" | replace "." "-" | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/*
Generate environment variables from service config
*/}}
{{- define "lazycloud-compose.envVars" -}}
{{- if .environment }}
{{- range $key, $value := .environment }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Generate volume mounts for a service
*/}}
{{- define "lazycloud-compose.volumeMounts" -}}
{{- if .volumes }}
{{- range .volumes }}
- name: {{ include "lazycloud-compose.sanitizeName" .name }}
  mountPath: {{ .mountPath }}
  {{- if .readOnly }}
  readOnly: {{ .readOnly }}
  {{- end }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Generate volumes for a service
*/}}
{{- define "lazycloud-compose.volumes" -}}
{{- if .volumes }}
{{- range .volumes }}
- name: {{ include "lazycloud-compose.sanitizeName" .name }}
  persistentVolumeClaim:
    claimName: {{ include "lazycloud-compose.sanitizeName" .name }}
{{- end }}
{{- end }}
{{- end }}