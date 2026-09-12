{{/*
Name of the Secret holding the RabbitMQ AMQP connection URL - the existing secret when
`rabbitmq.existingSecret` is set, otherwise the chart-managed one (see
market-prices-rabbitmq-secret.yaml). Used by the KEDA TriggerAuthentication for the market-prices
worker ScaledObjects.
*/}}
{{- define "eve-build.rabbitmqSecretName" -}}
{{- .Values.rabbitmq.existingSecret | default (printf "%s-rabbitmq-conn" .Release.Name) -}}
{{- end -}}

{{/*
Key within the RabbitMQ connection Secret holding the AMQP URL.
*/}}
{{- define "eve-build.rabbitmqSecretKey" -}}
{{- (ne .Values.rabbitmq.existingSecret "") | ternary .Values.rabbitmq.existingSecretKey "url" -}}
{{- end -}}

{{/*
The MONGODB_URI EnvVar entry for the app/workers, resolved the same way regardless of which
of the four supported modes is active (in-chart MongoDBCommunity, externalSecret,
existingSecret, or a plain `uri` value). Renders a single EnvVar entry at zero indentation.
*/}}
{{- define "eve-build.mongodbUriEnv" -}}
{{- if .Values.mongodb.enabled }}
- name: MONGODB_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-mongodb-connection
      key: connectionString.standard
{{- else if .Values.mongodb.externalSecret.enabled }}
- name: MONGODB_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-mongodb-external
      key: uri
{{- else if .Values.mongodb.existingSecret }}
- name: MONGODB_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.mongodb.existingSecret }}
      key: {{ .Values.mongodb.existingSecretKey }}
{{- else }}
- name: MONGODB_URI
  value: {{ .Values.mongodb.uri | quote }}
{{- end }}
{{- end -}}

{{/*
The MONGODB_URI EnvVar entry for the mongodb-exporter Deployment. When MongoDB is bundled
(`mongodb.enabled`), this points at the dedicated clusterMonitor-scoped user mongodb.yaml
creates for the exporter (see its "exporter.enabled" user block) - not the app's own
readWrite-on-one-database user, which lacks the privileges most collectors need. There's no
such dedicated user available when MongoDB is external/managed (this chart doesn't administer
it), so `mongodb.exporter.existingSecret` lets you point at a monitoring-scoped credential you
created there yourself; failing that, it falls back to `eve-build.mongodbUriEnv` (the app's own
credential) so the exporter still runs, with reduced metrics coverage until a proper credential
is supplied. Renders a single EnvVar entry at zero indentation.
*/}}
{{- define "eve-build.mongodbExporterUriEnv" -}}
{{- if .Values.mongodb.enabled }}
- name: MONGODB_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-mongodb-exporter-connection
      key: connectionString.standard
{{- else if .Values.mongodb.exporter.existingSecret }}
- name: MONGODB_URI
  valueFrom:
    secretKeyRef:
      name: {{ .Values.mongodb.exporter.existingSecret }}
      key: {{ .Values.mongodb.exporter.existingSecretKey }}
{{- else }}
{{- include "eve-build.mongodbUriEnv" . }}
{{- end }}
{{- end -}}

{{/*
Shared container env vars for every eve-build workload (the app Deployment, plus the
market-prices dispatch CronJob and fetch/write worker Deployments, and the tracked-info
dispatch CronJob and refresh-worker Deployment) - they all need the same Mongo/Redis/RabbitMQ/
ESI/SSO configuration. Renders a list of EnvVar entries at zero indentation; callers should
`{{- include "eve-build.env" . | nindent 12 }}` under their container's `env:` key.
*/}}
{{- define "eve-build.env" -}}
{{- $redisEnabled := or .Values.redis.enabled (ne .Values.redis.url "") }}
{{- $redisUrl := .Values.redis.enabled | ternary (printf "redis://%s-redis:6379/0" .Release.Name) .Values.redis.url }}
{{- $sessionSecretName := .Values.eveBuild.session.existingSecret | default (printf "%s-session" .Release.Name) }}
{{- $sessionSecretKey := (ne .Values.eveBuild.session.existingSecret "") | ternary .Values.eveBuild.session.existingSecretKey "secretKey" }}
{{- $marketPricesSecretName := .Values.eveBuild.marketPrices.existingSecret | default (printf "%s-market-prices" .Release.Name) }}
{{- $marketPricesSecretKey := (ne .Values.eveBuild.marketPrices.existingSecret "") | ternary .Values.eveBuild.marketPrices.existingSecretKey "apiKey" -}}
{{- if and .Values.mongodb.externalSecret.enabled (not .Values.mongodb.enabled) }}
- name: MONGODB_DATABASE
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-mongodb-external
      key: dbName
{{- else }}
- name: MONGODB_DATABASE
  value: {{ .Values.mongodb.database | quote }}
{{- end }}
{{- include "eve-build.mongodbUriEnv" . }}
- name: SDE_DATA_DIR
  value: {{ .Values.eveBuild.sdeDataDir | quote }}
- name: RUN_MIGRATIONS_ON_STARTUP
  value: {{ .Values.eveBuild.runMigrationsOnStartup | quote }}
- name: STARTUP_LOCK_TTL_SECONDS
  value: {{ .Values.eveBuild.startupLockTtlSeconds | quote }}
- name: STARTUP_LOCK_POLL_INTERVAL_SECONDS
  value: {{ .Values.eveBuild.startupLockPollIntervalSeconds | quote }}
- name: STARTUP_LOCK_WAIT_TIMEOUT_SECONDS
  value: {{ .Values.eveBuild.startupLockWaitTimeoutSeconds | quote }}
- name: REDIS_ENABLED
  value: {{ $redisEnabled | quote }}
{{- if $redisEnabled }}
- name: REDIS_URL
  value: {{ $redisUrl | quote }}
- name: REDIS_CACHE_TTL_SECONDS
  value: {{ .Values.redis.cacheTtlSeconds | int | quote }}
{{- end }}
- name: RABBITMQ_ENABLED
  value: {{ .Values.rabbitmq.enabled | quote }}
{{- if .Values.rabbitmq.enabled }}
{{- if .Values.rabbitmq.existingSecret }}
- name: RABBITMQ_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.rabbitmq.existingSecret }}
      key: {{ .Values.rabbitmq.existingSecretKey }}
{{- else }}
- name: RABBITMQ_URL
  value: {{ .Values.rabbitmq.url | quote }}
{{- end }}
{{- end }}
- name: METRICS_ENABLED
  value: {{ .Values.eveBuild.metrics.enabled | quote }}
- name: EVE_SSO_CLIENT_ID
  value: {{ .Values.eveBuild.eveSso.clientId | quote }}
- name: EVE_SSO_CALLBACK_URL
  value: {{ .Values.eveBuild.eveSso.callbackUrl | quote }}
- name: EVE_SSO_SCOPES
  value: {{ .Values.eveBuild.eveSso.scopes | quote }}
- name: EVE_SSO_CORP_SCOPES
  value: {{ .Values.eveBuild.eveSso.corpScopes | quote }}
- name: EVE_SSO_AUTHORIZE_URL
  value: {{ .Values.eveBuild.eveSso.authorizeUrl | quote }}
- name: EVE_SSO_TOKEN_URL
  value: {{ .Values.eveBuild.eveSso.tokenUrl | quote }}
- name: EVE_SSO_JWKS_URL
  value: {{ .Values.eveBuild.eveSso.jwksUrl | quote }}
- name: EVE_SSO_ISSUER
  value: {{ .Values.eveBuild.eveSso.issuer | quote }}
- name: EVE_SSO_AUDIENCE
  value: {{ .Values.eveBuild.eveSso.audience | quote }}
- name: ESI_BASE_URL
  value: {{ .Values.eveBuild.esi.baseUrl | quote }}
- name: ESI_COMPATIBILITY_DATE
  value: {{ .Values.eveBuild.esi.compatibilityDate | quote }}
- name: ESI_USER_AGENT
  value: {{ .Values.eveBuild.esi.userAgent | quote }}
- name: SESSION_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $sessionSecretName }}
      key: {{ $sessionSecretKey }}
- name: SESSION_COOKIE_NAME
  value: {{ .Values.eveBuild.session.cookieName | quote }}
- name: SESSION_MAX_AGE_SECONDS
  value: {{ .Values.eveBuild.session.maxAgeSeconds | int | quote }}
- name: MARKET_PRICES_REFRESH_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ $marketPricesSecretName }}
      key: {{ $marketPricesSecretKey }}
{{- with .Values.eveBuild.extraEnv }}
{{- toYaml . | nindent 0 -}}
{{- end }}
{{- end -}}
