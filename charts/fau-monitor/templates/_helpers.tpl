{{- define "fau-monitor.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "fau-monitor.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains .Chart.Chart.name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "fau-monitor.label" -}}
app.kubernetes.io/name: {{ include "fau-monitor.name" . }}
{{- if .Values.fullnameOverride }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- else }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
chart: {{ .Chart.Name }}
version: {{ .Chart.Version }}
app.kubernetes.io/release-name: {{ .Release.Name }}
app.kubernetes.io/release-creator: {{ .Release.Service }}
{{- end }}

{{- define "fau-monitor.selectorLabels" -}}
{{- include "fau-monitor.label" . | nindent 4 }}
{{- end }}

{{- define "fau-monitor.labelSelector" -}}
{{- include "fau-monitor.selectorLabels" . | nindent 4 }}
{{- end }}

{{- define "fau-monitor.matchLabels" -}}
{{- include "fau-monitor.selectorLabels" . | nindent 4 }}
{{- end }}

{{- define "fau-monitor.appLabelSelector" -}}
{{- include "fau-monitor.selectorLabels" . | nindent 4 }}
{{- end }}
