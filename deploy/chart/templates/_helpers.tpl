{{/*
The env block every workload shares.

One Secret, keyed by variable name, so this is a list of names rather than a
list of mappings. A variable named here and absent from the Secret leaves the
pod unable to start, which is the visible failure; a variable the process needs
and nobody named is the silent one, which is why the list is authored rather
than discovered.
*/}}
{{- define "lazycloud.env" -}}
{{- range $.Values.env }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $.Values.secrets.name }}
      key: {{ . }}
{{- end }}
{{- range $key, $value := $.Values.runtime }}
- name: {{ $key }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{/*
Refuse to render an image that was not pinned.

Helm's default for a missing value is the empty string, which renders a
Deployment whose image is `""` and fails at the pod with `InvalidImageName` --
several steps from the values file that forgot it.
*/}}
{{- define "lazycloud.image" -}}
{{- $ref := . -}}
{{- if not $ref -}}
{{- fail "an image digest was not supplied; the deploy renders these from what it pushed" -}}
{{- end -}}
{{- $ref -}}
{{- end -}}
