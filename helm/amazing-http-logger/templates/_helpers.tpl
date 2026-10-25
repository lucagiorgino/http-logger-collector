{{- define "logger.fullname" -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "logger.openbaoAddress" -}}
{{- if .Values.openbao.enabled -}}
{{- printf "http://%s-openbao:%d" (include "logger.fullname" .) (int .Values.openbao.port) -}}
{{- else -}}
{{- required "openbao.address is required for external OpenBao" .Values.openbao.address -}}
{{- end -}}
{{- end -}}

{{- define "logger.openbaoMounts" -}}
- name: openbao-auth
  {{- if .Values.openbao.auth.existingSecret }}
  mountPath: /run/secrets/openbao-credentials
  {{- else }}
  mountPath: /run/secrets/openbao-auth
  {{- end }}
  readOnly: true
{{- if .Values.openbao.ca.existingSecret }}
- name: openbao-ca
  mountPath: /run/secrets/openbao-ca
  readOnly: true
{{- end }}
{{- end -}}

{{- define "logger.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "logger.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "logger.mongoHosts" -}}
{{- $name := include "logger.fullname" . -}}
{{- $namespace := .Release.Namespace -}}
{{- $domain := .Values.mongodb.clusterDomain -}}
{{- $port := int .Values.mongodb.port -}}
{{- range $index := until 3 -}}
{{- if $index }},{{ end -}}
{{- printf "%s-mongodb-%d.%s-mongodb.%s.svc.%s:%d" $name $index $name $namespace $domain $port -}}
{{- end -}}
{{- end -}}

{{- define "logger.mongoEnv" -}}
- name: MONGO_URI
  {{- if .Values.mongodb.enabled }}
  value: {{ printf "mongodb://%s/?replicaSet=rs0" (include "logger.mongoHosts" .) | quote }}
  {{- else }}
  valueFrom:
    secretKeyRef:
      name: {{ required "externalMongo.existingSecret is required" .Values.externalMongo.existingSecret }}
      key: {{ .Values.externalMongo.uriKey }}
  {{- end }}
{{- end -}}

{{- define "logger.redisEnv" -}}
- name: REDIS_URL
  {{- if .Values.redis.enabled }}
  value: {{ printf "redis://%s-redis:%d/0" (include "logger.fullname" .) (int .Values.redis.port) | quote }}
  {{- else }}
  valueFrom:
    secretKeyRef:
      name: {{ required "externalRedis.existingSecret is required" .Values.externalRedis.existingSecret }}
      key: {{ .Values.externalRedis.urlKey }}
  {{- end }}
- name: REDIS_STREAM
  value: {{ .Values.queue.stream | quote }}
- name: REDIS_GROUP
  value: {{ .Values.queue.group | quote }}
- name: REDIS_MAX_BATCHES
  value: {{ .Values.queue.maxBatches | quote }}
- name: REDIS_SOCKET_TIMEOUT_SECONDS
  value: {{ .Values.queue.socketTimeoutSeconds | quote }}
- name: REDIS_AOF_TIMEOUT_MS
  value: {{ .Values.queue.aofTimeoutMs | quote }}
{{- end -}}
