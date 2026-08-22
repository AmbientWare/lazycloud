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
