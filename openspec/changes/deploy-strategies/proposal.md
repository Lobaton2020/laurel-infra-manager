---
slug: deploy-strategies
createdAt: 2026-08-17T02:09:16.603Z
---
## Why

Hoy `build_deployment` (`app/modules/scoops/manifest.py:108-127`) NO genera
`spec.strategy`, así que K8s aplica el default `RollingUpdate`
(`maxSurge: 25%`, `maxUnavailable: 25%`) y esa es la **única** estrategia de
deploy disponible. Para scoops críticos (API pública, base de datos de
negocio) eso es limitante:

1. **Sin rollback inmediato.** Un bad release se propaga gradualmente y no
   hay forma de volver atrás de golpe; el rollback es un redeploy manual de
   la versión anterior.
2. **Sin conmutación controlada.** No se puede decidir "la nueva versión
   empieza a recibir tráfico cuando yo lo diga" (blue-green), ni validar
   una fracción del tráfico antes de promover (canary), ni provocar un
   downtime deliberado (recreate).
3. **Sin knobs de rollout.** No se puede ajustar `maxSurge`/`maxUnavailable`
   por scoop desde la plataforma.

El cambio agrega un campo `Scoop.deploy_strategy` (`rolling|blue_green|
canary|recreate`, default `rolling`) y el soporte en el pipeline de deploy
para las cuatro estrategias, con implementación por fases.

## What Changes

- `Scoop.deploy_strategy`: enum `rolling|blue_green|canary|recreate`,
  default `rolling`. Nuevo campo en el modelo, schemas, service (update y
  tracked) y controller (`POST /api/scoops/<id>/deploy` acepta
  `strategy`/`canary_replicas` opcionales).
- `ManifestService.build_deployment` recibe `name_suffix`/`gen` y emite
  `spec.strategy`; para blue-green/canary sufija el nombre del Deployment y
  agrega una label discriminatoria inmutable `laurel.io/gen=<version>`
  (el selector de un Deployment es inmutable post-creación).
- `ManifestService.build_service`: para canary el selector omite la label
  `gen` (matchea ambas generaciones); para blue-green el Service conserva el
  nombre estable `<name>` y el selector se parchea (PATCH atómico) en la
  conmutación.
- `ManifestService.build_hpa`: `scaleTargetRef` dinámico (apunta al gen
  activo) o HPA por gen.
- `DeployService.deploy`: dispatch por estrategia:
  - `rolling` / `recreate` → bucle create/replace idempotente actual
    (`deploy.py:46-112`).
  - `blue_green` → (1) create/replace Deployment `vN+1` sin tráfico →
    (2) esperar `ready >= desired` (reusar lógica de status
    `deploy.py:179-193`) → (3) PATCH selector del Service al gen nuevo
    (switch sin downtime) → (4) borrar `vN` o conservarlo para rollback.
  - `canary` → Deployment `<name>-canary` + Service cuyo selector NO incluye
    `gen` (traffic = proporción de réplicas; los pods se gatean por
    readinessProbe) + promoción = replace del Deployment primario + scale
    canary a 0.
- Cleanup de generaciones residuales en `undeploy` y en el delete del scoop
  (el check de deploy activo hoy consulta por `scoop.name`; con nombres
  sufijados hay que listar por label `app=<name>`).
- `status` (`deploy.py:141-207`): hoy lee `read(scoop.name, ns)`; en
  blue-green resuelve el gen activo leyendo el selector del Service (fuente
  de verdad en el cluster) y reporta ambos Deployments en `pods`.
- Jenkins: sin cambios — solo build+push de imagen + PUT de version +
  POST deploy; la estrategia vive en el scoop.

## Capabilities

### New Capabilities

- `deploy-strategies`: soporte en el pipeline de deploy de Laurel-infra-
  manager para las estrategias `rolling` (default, actual), `recreate`,
  `blue_green` y `canary`, seleccionables por scoop vía el campo
  `Scoop.deploy_strategy` y por request en `POST /api/scoops/<id>/deploy`.

### Modified Capabilities

- `scoops`: el modelo gana la columna `deploy_strategy` (default
  `"rolling"`); `ScoopCreate`/`ScoopUpdate`/`ScoopResponse` y `DeployRequest`
  ganan `strategy` y `canary_replicas` opcionales; `ScoopService.update` y
  tracked lo tratan como campo escalar. `DeployService.deploy` despacha por
  estrategia. `undeploy`/delete limpian generaciones residuales.

## Impact

**Backend:**
- 1 módulo modificado: `scoops` (`model.py`, `schema.py`, `service.py`,
  `manifest.py`, `deploy.py`, `controller.py`).
- `DeployService.deploy` se convierte en dispatcher por estrategia; se
  agregan helpers de espera-ready, PATCH de selector y cleanup de
  generaciones.
- Nueva migración: columna `scoops.deploy_strategy` (default `"rolling"`),
  sin backfill (todos los scoops existentes quedan en `rolling`).

**Frontend:** ninguno en esta iteración (los knobs se exponen vía API;
UI opcional post-change si un flujo real lo pide).

**Dependencias nuevas:** ninguna.

**Cluster:**
- Scoops en `blue_green`/`canary` generan Deployments con nombre sufijado
  (`<name>-<gen>`, `<name>-canary`), acotado a 63 chars DNS-1123 (gen
  truncado con `_sanitize_label`).
- El Service conserva nombre `<name>` y puerto estable → Ingress/DNS
  (`Domain` → Service) transparente. Canary por Ingress (weight) queda fuera
  de scope: rompe el contrato Domain→Service y requiere annotations traefik.

**Riesgos:**
- Selector inmutable del Deployment: la label `gen` se fija al crear y nunca
  se edita; el switch blue-green es un PATCH al selector del Service, no al
  Deployment.
- Cleanup de generaciones: `undeploy`/delete actuales borran solo por
  `scoop.name`; hay que listar por label `app=<name>` para no dejar
  Deployments huérfanos.
- HPA + blue-green: solo el gen activo escala; el viejo se congela o se
  borra.

## Follow-up status

**Spec-only change.** Esta propuesta documenta la investigación y el plan;
la implementación se retoma en un sprint futuro (ver `tasks.md`, todo sin
ejecutar). No hay fase de implementación en este change.
