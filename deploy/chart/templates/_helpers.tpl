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
