---
slug: app-platform-namespace-per-app
createdAt: 2026-08-17T00:35:25.965Z
---
## Why

Laurel-infra-manager trata los scoops como unidades independientes dentro de un
namespace compartido (`user-apps` por convención). Esto tiene tres limitaciones
operativas que ya rozan el flujo de uso real:

1. **Sin aislamiento entre apps.** Dos apps distintas conviven en el mismo
   namespace: comparten DNS overrides, accidentalmente ven ConfigMaps/Secrets
   ajenas vía `envFrom`, y un `kubectl delete namespace` para limpiar una app
   tumbaría a todas las demás.
2. **Sin identidad de aplicación.** El campo `Scoop.application` es un string
   libre; no hay una entidad "App" de primer nivel que agrupe los scoops
   (api/worker/cronjob) ni que tenga su propio ciclo de vida, ni metadata
   externa (repo, registry, secretos).
3. **Sin bootstrap externo.** Crear una app requiere pasos manuales en GitHub
   y Docker Hub que no quedan registrados; no hay forma de saber desde
   la plataforma qué repos están vivos, ni de recrear el repo si se pierde.

El cambio introduce una entidad `Application` de primer nivel, le asocia un
namespace dedicado por app (1 app → 1 namespace → N scoops), y agrega dos
integraciones externas mínimas (GitHub + Docker Hub) autenticadas por PAT para
que el bootstrap de una app nueva sea un solo flujo.

## What Changes

- Nuevo módulo `app/modules/apps/` con modelo `Application` (id, name, slug,
  description, github_repo_url, docker_image_base, created_at, deleted_at) +
  CRUD + servicio de lifecycle. **`Application` NO incluye dominio propio:**
  un dominio es un recurso separado que se asocia a un scoop después de
  que la app y los scoops existen.
- `DELETE /api/apps/<id>?force=true` elimina en cascada todos los recursos del
  namespace (Deployment, Service, Ingress, Certificate, ConfigMaps, Secrets,
  **Domains**) + el namespace mismo, y marca los scoops huérfanos en BD como
  `archived`.
- **Nuevo módulo `app/modules/domains/`** (recurso de primer nivel): modelo
  `Domain` con FKs a `Application` y `Scoop`, campo `host` (subdominio
  único), `tls` (default `true`), `status`, `secret_name`. Servicio de
  lifecycle propio (`deploy`, `undeploy`, `status`, `certificate_status`).
  **El Ingress y el Certificate YA NO se generan automáticamente al deploy
  del scoop** — se generan al deploy del `Domain` que referencia ese scoop.
- Refactor: `ManifestService.build()` deja de emitir `Ingress` y
  `Certificate`. `Scoop.host`/`Scoop.url` desaparecen del response.
  El `DNS override` de CoreDNS pasa a ser responsabilidad de `DomainService`,
  no de `DeployService`.
- Nuevo módulo `app/modules/integrations/github/` (cliente `requests`,
  autenticado por PAT) con `create_empty_repo(name, private)` y
  `repo_exists(owner, name)`. Token configurable vía secret del sistema.
- Nuevo módulo `app/modules/integrations/docker/` (cliente `requests`
  autenticado por PAT) con `validate_image(image_ref)` y
  `image_exists(image_ref)`. Sin push automático: solo lectura + validación.
- `Scoop` agrega FK opcional `application_id`; cuando está presente, el
  namespace del scoop se deriva del slug de la app (`<slug>`), salvo override
  explícito en `Scoop.namespace`. Backfill automático en migración BD:
  agrupa scoops existentes por `application` y crea una `Application` por
  grupo único.
- `app/modules/configstore/`: el label `app=<slug>` ahora se vincula a la
  `Application` por FK además del label. `app` en el body del request pasa a
  resolverse contra `Application.slug` con error 404 si no existe.
- `app/modules/system/`: agrega dos secretos administrados:
  `github_pat` (text) y `docker_pat` (text).
- Frontend: nueva página `Apps.tsx` con lista/crear/eliminar apps; integra
  el form de creación con toggle "crear repo en GitHub". `ScoopNew.tsx`
  cambia el input libre de `application` por un dropdown que lista apps
  existentes (con fallback a texto libre si no hay apps). **Nueva página
  `Domains.tsx`** con lista/crear/eliminar dominios y deploy de Ingress/TLS
  por dominio. **ScoopDetail** ya no muestra host/certificado (esos viven
  en el detalle del Domain).

## Capabilities

### New Capabilities

- `apps`: Modelo de primer nivel "Application" con CRUD, lifecycle de
  namespace dedicado (creación idempotente al deploy, borrado en cascada
  con `?force=true`), y binding opcional a repo GitHub / imagen Docker.
- `domains`: Recurso de primer nivel "Domain" que asocia un subdominio
  público (`host`) a exactamente un `Scoop` (y por transitividad a su
  `Application`). Genera Ingress + Certificate (LetsEncrypt) + DNS
  override cuando se deploya. **No autogenerado al crear el scoop**:
  se crea como paso separado cuando el usuario decide exponer el scoop.
- `integrations-github`: Cliente PAT para GitHub. Operaciones expuestas:
  `create_empty_repo`, `repo_exists`. Sin OAuth, sin webhooks.
- `integrations-docker-hub`: Cliente PAT para Docker Hub. Operaciones
  expuestas: `validate_image_ref`, `image_exists`. Sin push automático,
  sin build.

### Modified Capabilities

- `scoops`: el campo `namespace` deja de ser fuente única; cuando
  `application_id` está set, se deriva del slug de la app salvo override.
  **`Scoop` ya NO incluye `host`, `url`, ni auto-genera Ingress/Certificate`
  en el deploy — eso es responsabilidad del recurso `Domain`**.
  Scoops quedan agrupados por app en listados y filtros.
- `configstore`: el campo `app` se valida contra `Application.slug` (404
  si no existe); los recursos ConfigMap/Secret se vinculan a la app por
  FK además del label.
- `system-secrets`: la whitelist de secretos administrados crece con
  `github_pat` y `docker_pat` (kind: text).

## Impact

**Backend:**
- 4 módulos nuevos: `apps`, `domains`, `integrations/github`,
  `integrations/docker`.
- 4 módulos modificados: `scoops`, `configstore`, `system`, `cluster`.
- Refactor de `ManifestService.build()`: ya no emite Ingress/Certificate.
  Refactor de `DeployService.deploy()`: ya no aplica DNS override.
- Nueva migración: `applications` + `domains` tables + backfill
  + FKs en `scoops.application_id`, `domains.application_id`,
  `domains.scoop_id`.
- Nuevos endpoints: `GET/POST /api/apps`, `GET/PUT/DELETE /api/apps/<id>`,
  `DELETE /api/apps/<id>?force=true`, `GET/POST /api/domains`,
  `GET/PUT/DELETE /api/domains/<id>`, `POST /api/domains/<id>/deploy`,
  `DELETE /api/domains/<id>/deploy`, `GET /api/domains/<id>/status`,
  `GET /api/domains/<id>/certificate`, `GET /api/domains/<id>/certificate/logs`.
- Auditoría: cada transición de lifecycle (create/update/delete-force) se
  registra vía `AuditService.log`.

**Frontend (configurator-lob):**
- 2 páginas nuevas: `src/pages/Apps.tsx`, `src/pages/Domains.tsx`.
- 2 páginas modificadas: `src/pages/ScoopNew.tsx`,
  `src/components/Layout.tsx`. `src/pages/ScoopDetail.tsx` pierde la
  sección de certificado (movida a `DomainDetail`).
- 2 módulos API nuevos: `src/api/apps.ts`, `src/api/domains.ts`.
- 1 componente nuevo opcional: `GitHubRepoToggle` dentro de Apps.tsx.

**Dependencias nuevas:**
- `requests` (probablemente ya presente; verificar `requirements.txt`).
- No se agregan clientes SDK pesados; usamos `requests` directo.

**Cluster:**
- A partir del cambio, cada app nueva genera su propio namespace en k3s.
- El namespace `user-apps` sigue siendo el default legacy para scoops sin
  `application_id` (compatibilidad). Scoops existentes siguen funcionando
  sin cambios hasta que se migren manualmente.
- `ClusterIssuer letsencrypt-prod` sigue siendo el emisor (sin cambios).

**Riesgos:**
- Backfill de BD: si dos apps existentes sluggean al mismo valor, una falla.
  Mitigation: validación pre-migración + rollback explícito.
- PAT leak: si el log captura `Authorization`, queda expuesto. Mitigation:
  sanitizar headers en `AuditService.log`.
- Delete cascade: si k3s tiene pods stuck en `Terminating`, el endpoint
  puede colgarse. Mitigation: timeout explícito + reporte de progreso parcial.