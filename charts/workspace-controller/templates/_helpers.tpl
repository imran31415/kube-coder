{{/*
Common name for every workspace-controller resource. Fixed (not per-user):
this chart is a namespace singleton.
*/}}
{{- define "wc.name" -}}
workspace-controller
{{- end -}}

{{- define "wc.labels" -}}
app: workspace-controller
{{- end -}}
