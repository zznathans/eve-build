# eve-build

Helm chart for [`eve-build`](https://github.com/zznathans/eve-build).

Deploys a FastAPI Deployment + Service, exposing `/` and `/health`. No
Ingress by default — the Service is meant to sit behind whatever
externally-managed ingress/traffic routing your cluster already uses.
Optionally (`eveBuild.ingress.enabled`) the chart deploys its own Ingress
instead, routing `eveBuild.ingress.host` to the Service - set
`eveBuild.ingress.className` for a non-default IngressClass, and
`eveBuild.ingress.tls.enabled` (with `eveBuild.ingress.tls.secretName`) to
terminate TLS there, typically paired with a
`cert-manager.io/cluster-issuer` annotation under
`eveBuild.ingress.annotations` for automatic certificate issuance.

Optionally (`mongodb.enabled`) also deploys a `MongoDBCommunity` custom
resource — a MongoDB replica set. This requires the
[MongoDB Community Kubernetes Operator](https://github.com/mongodb/mongodb-kubernetes-operator)
already installed in the cluster; this chart only creates the CR, not the
operator itself. The user's password comes from a Secret (a `password` key):
set `mongodb.passwordSecretName` to point at one you already manage (e.g.
External Secrets, or `kubectl create secret generic ... --from-literal=password=...`),
or leave it empty and the chart generates and manages one itself — a random
password, generated once and kept stable across upgrades. When
`mongodb.enabled` is false, the app instead connects using
`mongodb.uri` (a plain connection string, e.g. for an external/managed
MongoDB instance), or a Secret referenced via `mongodb.existingSecret`, or
one synced by an optionally chart-deployed `ExternalSecret`
(`mongodb.externalSecret.enabled`, requires the
[External Secrets Operator](https://external-secrets.io) already installed
in the cluster) - which pulls a JSON object with `db_host`, `db_host_public`,
`db_port`, `db_user`, `db_password`, and `db_name` keys (the shape DigitalOcean Managed
MongoDB writes) and assembles it into a connection string.

Optionally (`redis.enabled`) also deploys a plain Redis Deployment + Service,
used by the app as an optional cache to cut down on MongoDB reads for
reference data. It's not a hard dependency of the app — nothing else in the
cluster needs to know about it, and there's no persistence/PVC since it's
purely a cache. Point the app at an external Redis instead via `redis.url`
(leaving `redis.enabled` false). Optionally (`redis.exporter.enabled`) also
runs a `redis_exporter` sidecar in the Redis pod, exposing Prometheus-compatible
metrics; `redis.exporter.serviceMonitor.enabled` deploys a `ServiceMonitor` for
it, same as the app's own (requires the Prometheus Operator CRDs).

Optionally (`rabbitmq.enabled`) the app connects to a RabbitMQ instance —
this chart doesn't deploy RabbitMQ itself, only wires the connection up:
point `rabbitmq.url` at an external/managed instance, or reference a Secret
via `rabbitmq.existingSecret` holding the AMQP URL. Leave `rabbitmq.enabled`
false (the default) if the app has no messaging use configured yet.

Optionally (`eveBuild.metrics.enabled`) the app exposes Prometheus-compatible
metrics at `GET /metrics`. Optionally (`eveBuild.metrics.serviceMonitor.enabled`)
also deploys a `ServiceMonitor` so a Prometheus Operator in the cluster scrapes it
automatically — this requires the
[Prometheus Operator](https://github.com/prometheus-operator/prometheus-operator)
CRDs already installed in the cluster.

The app's full configuration surface (EVE Online SSO, ESI, session cookie
settings, SDE import behavior) is exposed under `eveBuild.eveSso`, `eveBuild.esi`,
`eveBuild.session`, `eveBuild.sdeDataDir`, and `eveBuild.runMigrationsOnStartup` — see
the values table below. `eveBuild.session.secretKey` (which signs the session
cookie) is required unless `eveBuild.session.existingSecret` is set; the chart
stores it in a Secret rather than inlining it into the Deployment spec.
Standard scheduling/workload knobs (`resources`, `securityContext`,
`nodeSelector`, `tolerations`, `affinity`, `podAnnotations`, `podLabels`,
`imagePullPolicy`, `extraEnv`) are also all configurable per the values
table.

## Values

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| eveBuild.affinity | object | `{}` | Affinity rules for the pod. |
| eveBuild.esi.baseUrl | string | `"https://esi.evetech.net"` | Base URL for the EVE Swagger Interface. |
| eveBuild.esi.compatibilityDate | string | `"2026-08-18"` | ESI compatibility date header value. |
| eveBuild.esi.userAgent | string | `"eve-build"` | User-Agent sent on ESI requests - identify your deployment per ESI's guidelines. |
| eveBuild.eveSso.audience | string | `"EVE Online"` | Expected `aud` claim on SSO tokens. |
| eveBuild.eveSso.authorizeUrl | string | `"https://login.eveonline.com/v2/oauth/authorize"` | EVE SSO authorization endpoint. |
| eveBuild.eveSso.callbackUrl | string | `""` | Callback URL registered on the SSO application. Must match exactly. |
| eveBuild.eveSso.clientId | string | `""` | EVE Online SSO application client ID (https://developers.eveonline.com/applications). |
| eveBuild.eveSso.corpScopes | string | `""` | Space-separated ESI scopes for the optional, separate "connect corporation data" grant (see /settings in the app) - e.g. esi-assets.read_corporation_assets.v1, esi-corporations.read_blueprints.v1, esi-industry.read_corporation_jobs.v1. Requested only when a character opts in, not during base login. May be empty (feature stays unusable until set, but the app runs fine without it). |
| eveBuild.eveSso.issuer | string | `"https://login.eveonline.com"` | Expected `iss` claim on SSO tokens. |
| eveBuild.eveSso.jwksUrl | string | `"https://login.eveonline.com/oauth/jwks"` | EVE SSO JWKS endpoint, used to verify token signatures. |
| eveBuild.eveSso.scopes | string | `""` | Space-separated ESI scopes to request during login. May be empty. |
| eveBuild.eveSso.tokenUrl | string | `"https://login.eveonline.com/v2/oauth/token"` | EVE SSO token endpoint. |
| eveBuild.extraEnv | list | `[]` | Extra environment variables appended to the app container, after the chart's own. Each entry is a raw Kubernetes EnvVar (`name` plus `value` or `valueFrom`) - use this for anything not otherwise exposed below. |
| eveBuild.extraObjects | list | `[]` | Raw Kubernetes objects to render alongside chart-managed resources. |
| eveBuild.imagePullPolicy | string | `"IfNotPresent"` | Image pull policy for the app container. |
| eveBuild.imagePullSecrets | list | `[]` | List of image pull secret names to attach to the ServiceAccount. Leave empty if the registry is public. |
| eveBuild.imageRepository | string | `"ghcr.io/zznathans/eve-build"` | Container image registry and repository for the eve-build image. |
| eveBuild.imageTag | string | `""` | Image tag to deploy. Empty by default: falls back to .Chart.AppVersion, which chart-publish.yml overrides to match the release a published chart was packaged from - so installing a published chart with no override deploys the matching image automatically. Only matters as a literal default for a raw checkout (falls back to Chart.yaml's committed appVersion) or local helm lint/unittest. |
| eveBuild.ingress.annotations | object | `{}` | Extra annotations on the Ingress - e.g. cert-manager.io/cluster-issuer, to have cert-manager automatically issue the TLS certificate referenced by ingress.tls.secretName below. |
| eveBuild.ingress.className | string | `""` | IngressClass to use (e.g. "traefik"). Empty uses the cluster's default IngressClass. |
| eveBuild.ingress.enabled | bool | `false` | Create an Ingress routing to this app's Service. Off by default - see the chart README for why (the Service is meant to sit behind whatever externally-managed ingress/traffic routing your cluster already uses; this is an opt-in alternative for clusters that route via a Kubernetes Ingress controller instead). |
| eveBuild.ingress.host | string | `""` | Hostname to route to this app. Required when enabled. |
| eveBuild.ingress.tls.enabled | bool | `false` | Terminate TLS on the Ingress. Typically paired with a cert-manager.io/cluster-issuer annotation above so the certificate is issued automatically. |
| eveBuild.ingress.tls.secretName | string | `""` | Name of the Secret holding the TLS certificate. When using cert-manager, this is the Secret it creates/manages - pick any name. |
| eveBuild.marketPrices.existingSecret | string | `""` | Name of an existing Secret holding the refresh API key, used instead of `refreshApiKey` when set. |
| eveBuild.marketPrices.existingSecretKey | string | `"apiKey"` | Key within `existingSecret` holding the refresh API key. |
| eveBuild.marketPrices.refreshApiKey | string | `""` | Shared secret the on-demand `/market-prices/refresh` endpoint requires via the `X-Api-Key` header. Prices also refresh automatically every hour as part of the market_orders dispatch run (when rabbitmq.enabled) - this endpoint is only for triggering an out-of-cycle refresh manually. Required unless `existingSecret` is set - generate one with `openssl rand -hex 32`. |
| eveBuild.metrics.enabled | bool | `false` | Expose Prometheus-compatible metrics at GET /metrics. |
| eveBuild.metrics.serviceMonitor.enabled | bool | `false` | Deploy a ServiceMonitor for Prometheus Operator to scrape /metrics automatically. Requires the Prometheus Operator CRDs already installed in the cluster, and `eveBuild.metrics.enabled: true`. |
| eveBuild.metrics.serviceMonitor.interval | string | `"30s"` | Scrape interval. |
| eveBuild.metrics.serviceMonitor.labels | object | `{}` | Extra labels on the ServiceMonitor, e.g. for a Prometheus instance's serviceMonitorSelector. |
| eveBuild.nodeSelector | object | `{}` | Node selector for the pod. |
| eveBuild.podAnnotations | object | `{}` | Annotations applied to the pod template. |
| eveBuild.podLabels | object | `{}` | Extra labels applied to the pod template, in addition to the chart's own `app` label. |
| eveBuild.replicaCount | int | `1` | Number of pod replicas. The app is stateless, so this is safe to scale up freely. |
| eveBuild.resources | object | `{"limits":{"cpu":"250m","memory":"128Mi"},"requests":{"cpu":"50m","memory":"64Mi"}}` | Resource requests and limits for the app container. |
| eveBuild.runMigrationsOnStartup | bool | `true` | Run pending database migrations (including the initial SDE import) automatically on startup. |
| eveBuild.sdeDataDir | string | `"app/data/sde"` | Directory the bundled EVE SDE data is read from at migration time. Only override this if you're mounting a custom SDE dataset. |
| eveBuild.securityContext | object | `{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]},"runAsNonRoot":true,"runAsUser":1000,"seccompProfile":{"type":"RuntimeDefault"}}` | Container securityContext for the app container. |
| eveBuild.service.port | int | `80` | Port the Service listens on and forwards to the container's 8000. |
| eveBuild.session.cookieName | string | `"eve_build_session"` | Name of the session cookie. |
| eveBuild.session.existingSecret | string | `""` | Name of an existing Secret holding the session signing key, used instead of `secretKey` when set. |
| eveBuild.session.existingSecretKey | string | `"secretKey"` | Key within `existingSecret` holding the session signing key. |
| eveBuild.session.maxAgeSeconds | int | `1209600` | Session cookie lifetime, in seconds. |
| eveBuild.session.secretKey | string | `""` | Value used to sign the session cookie. Required unless `existingSecret` is set - generate one with `openssl rand -hex 32`. The chart stores it in a Secret rather than inlining it into the Deployment spec. |
| eveBuild.tolerations | list | `[]` | Tolerations for the pod. |
| mongodb.database | string | `"eve-build"` | Database the user is scoped to, via a readWrite role, and the database name the app is told to use (MONGODB_DATABASE). Ignored when `externalSecret.enabled` is true - in that mode the database name comes from the external secret's own `db_name` field instead, so it can't drift out of sync with what the assembled connection URI points at. |
| mongodb.enabled | bool | `false` | Deploy a MongoDBCommunity custom resource for this release. Requires the MongoDB Community Kubernetes Operator (https://github.com/mongodb/mongodb-kubernetes-operator) already installed in the cluster - this chart only creates the CR, not the operator itself. |
| mongodb.existingSecret | string | `""` | Name of an existing Secret holding a MongoDB connection string, used instead of `uri` when set - e.g. via External Secrets. Ignored when `enabled` or `externalSecret.enabled` is true. |
| mongodb.existingSecretKey | string | `"uri"` | Key within `existingSecret` holding the connection string. |
| mongodb.externalSecret.enabled | bool | `false` | Deploy an ExternalSecret (https://external-secrets.io) that syncs MongoDB connection details from an external secret store into a Secret this chart's Deployment then reads - an alternative to `existingSecret` when you'd rather have the chart manage the ExternalSecret object itself. Requires the External Secrets Operator already installed in the cluster. Ignored when `mongodb.enabled` is true; takes precedence over `existingSecret`/`uri` when set. |
| mongodb.externalSecret.refreshInterval | string | `"1h"` | How often the ExternalSecret refreshes from the store. |
| mongodb.externalSecret.remoteKey | string | `""` | Key/path within the external store holding the connection details. The remote secret is expected to be a JSON object with `db_host`, `db_host_public`, `db_port`, `db_user`, `db_password`, and `db_name` keys - the shape DigitalOcean Managed MongoDB writes - which are assembled into a `mongodb+srv://` connection string (`db_port` is unused: DigitalOcean's managed MongoDB hosts only resolve via SRV/TXT records, not a plain A record, so the actual host:port pairs and replica set name come from DNS instead of being embedded in the URI). |
| mongodb.externalSecret.secretStoreKind | string | `"SecretStore"` | Kind of the store referenced above - SecretStore or ClusterSecretStore. |
| mongodb.externalSecret.secretStoreRef | string | `""` | Name of the (Cluster)SecretStore to pull from. |
| mongodb.externalSecret.uriOptions | string | `"authSource=admin&tls=true"` | Query string appended to the assembled connection string (e.g. authSource, tls). Leave empty to omit. DigitalOcean Managed MongoDB requires TLS and an admin authSource. |
| mongodb.externalSecret.usePublicHost | bool | `false` | Use `db_host_public` instead of `db_host` when assembling the connection string - e.g. when the app isn't on the same VPC/network as the database and can only reach it over its public endpoint. |
| mongodb.members | int | `3` | Number of replica set members. |
| mongodb.passwordSecretName | string | `""` | Name of an existing Secret holding the user's password under a `password` key - e.g. via External Secrets, or `kubectl create secret generic <name> --from-literal=password=...`. Leave empty to have the chart generate and manage a random password itself, in a Secret named "<release-name>-mongodb-password" - generated once and kept stable across upgrades (reuses the existing Secret's password if there is one). |
| mongodb.resources | object | `{"limits":{"cpu":"500m","memory":"512Mi"},"requests":{"cpu":"100m","memory":"256Mi"}}` | Resource requests and limits for the mongod container. |
| mongodb.storage | string | `"10Gi"` | Storage requested per member's PersistentVolumeClaim. |
| mongodb.uri | string | `"mongodb://localhost:27017"` | Plain MongoDB connection string for the app to use when `enabled` is false and `existingSecret` isn't set - e.g. an external/managed MongoDB instance outside the cluster. Ignored when `enabled` is true (the in-cluster MongoDBCommunity's own connection string is used instead). |
| mongodb.username | string | `"eve-build"` | Database user the operator creates. |
| mongodb.version | string | `"7.0.14"` | MongoDB server version to run. |
| rabbitmq.enabled | bool | `false` | Enable RabbitMQ connectivity for the app (sets RABBITMQ_ENABLED and wires up RABBITMQ_URL below). This chart does not deploy RabbitMQ itself - point `url` (or `existingSecret`) at an external/managed RabbitMQ instance. Leave disabled if the app has no messaging use yet. |
| rabbitmq.existingSecret | string | `""` | Name of an existing Secret holding the AMQP connection URL, used instead of `url` when set - e.g. via External Secrets. |
| rabbitmq.existingSecretKey | string | `"url"` | Key within `existingSecret` holding the connection URL. |
| rabbitmq.url | string | `"amqp://guest:guest@localhost:5672/"` | AMQP connection URL for the app to use when `existingSecret` isn't set - e.g. an external/managed RabbitMQ instance. |
| redis.cacheTtlSeconds | int | `86400` | How long cached SDE/location lookups are kept, in seconds. Only relevant when Redis is enabled (bundled or external). |
| redis.enabled | bool | `false` | Deploy a Redis instance for this release. The app uses it as an optional read-through cache for reference data (SDE type/blueprint lookups, resolved location names) to cut down on MongoDB reads - it's not a hard dependency, the app falls back to querying MongoDB directly if Redis is disabled or unreachable. |
| redis.exporter.enabled | bool | `false` | Deploy a redis_exporter sidecar alongside the bundled Redis instance, exposing Prometheus-compatible metrics at :9121/metrics. Only applies when `redis.enabled` is true (bundled Redis) - there's nothing to sidecar onto for an external Redis. |
| redis.exporter.resources | object | `{"limits":{"cpu":"50m","memory":"32Mi"},"requests":{"cpu":"10m","memory":"16Mi"}}` | Resource requests and limits for the redis-exporter container. |
| redis.exporter.serviceMonitor.enabled | bool | `false` | Deploy a ServiceMonitor for Prometheus Operator to scrape the exporter automatically. Requires the Prometheus Operator CRDs already installed in the cluster, and `redis.exporter.enabled`. |
| redis.exporter.serviceMonitor.interval | string | `"30s"` | Scrape interval. |
| redis.exporter.serviceMonitor.labels | object | `{}` | Extra labels on the ServiceMonitor (e.g. for a Prometheus instance's serviceMonitorSelector). |
| redis.exporter.version | string | `"v1.66.0"` | redis_exporter image tag to run. |
| redis.resources | object | `{"limits":{"cpu":"100m","memory":"128Mi"},"requests":{"cpu":"25m","memory":"32Mi"}}` | Resource requests and limits for the redis container. |
| redis.url | string | `""` | External Redis URL for the app to use when `enabled` is false but you still want the read-through cache backed by a Redis instance outside the cluster. Ignored (and REDIS_ENABLED left off) when both this and `enabled` are unset/false. |
| redis.version | string | `"7.4-alpine"` | Redis image tag to run. |

## Development

```
helm lint charts/eve-build --strict
helm unittest charts/eve-build
```
