## Context

- Laurel-infra-manager es una API Flask monolítica con módulos por dominio
  (`scoops`, `configstore`, `system`, `cluster`); cada módulo sigue el
  patrón `model.py + schema.py + service.py + controller.py`.
- K8s (k3s `homelob`): la API Python usa el cliente `kubernetes` oficial
  vía `K8sService` (CRUD genérico). El deploy del scoop es
  create/replace idempotente con manejo de carrera 409
  (`app/modules/scoops/deploy.py:46-112`).
- `ManifestService.build_deployment` (`app/modules/scoops/manifest.py:108-127`)
  NO emite `spec.strategy` → K8s aplica `RollingUpdate` default
  (`maxSurge: 25%`, `maxUnavailable: 25%`). Selector estable
  `app: <name>` (`manifest.py:41-51`), label `version` sanitizada
  (`manifest.py:53-59`), probes (`98-103`), HPA v2 (`155-183`),
  `envFrom` (`271-349`), auto-creación de namespace (`deploy.py:51-57`).
- El status del scoop (`deploy.py:141-207`) lee `read(scoop.name, ns)` y
  espera `ready >= desired` (`deploy.py:179-193`).
- `undeploy` borra por `scoop.name`; el delete del scoop
  (`controller.py:205-253`) chequea deploy activo con
  `exists("Deployment", ns, scoop.name)`.
- Ingress/DNS viven en el recurso `Domain` (post refactor
  `app-platform-namespace-per-app`): el Service del scoop es el punto de
  unión estable `Domain → Service`.

## Goals / Non-Goals

**Goals:**
- Campo `Scoop.deploy_strategy` (`rolling|blue_green|canary|recreate`,
  default `rolling`) como fuente de verdad de la estrategia por scoop.
- `recreate` y `rolling custom` (`maxSurge`/`maxUnavailable` desde config)
  como cambio mínimo (~10 líneas en el manifest).
- Blue-green: dos Deployments `<name>-v<N>`/`<name>-v<N+1>` conviviendo,
  UN solo Service estable `<name>` cuyo selector se PATCHea (switch atómico
  sin downtime).
- Canary por proporción de réplicas (Service sin label `gen` en el selector
  → tráfico proporcional a pods Ready), con promoción por replace + scale a 0.
- Cleanup de generaciones residuales en undeploy y delete del scoop.
- Backwards-compatible: `deploy_strategy` default `rolling` = comportamiento
  actual sin cambios.
- Tests automatizados por estrategia (mock de `K8sService`).

**Non-Goals:**
- Switch blue-green programado ("fecha de conmutación"): requeriría campo
  `switch_at` + background job → out of scope del v1.
- Canary por Ingress/route con weight (requiere annotations traefik y rompe
  el contrato `Domain → Service`).
- Service Mesh (Istio/Linkerd) para traffic splitting.
- Frontend / UI de estrategias en esta iteración (se expone vía API).
- Blue-green con dos Services + doble Ingress.

## Decisions

### D1. `Scoop.deploy_strategy` como enum con default `rolling`

`Scoop.deploy_strategy` es una columna string con default `"rolling"` y
validación de enum en schema. `rolling` reproduce exactamente el
comportamiento actual (sin `spec.strategy` explícito, o emitiéndolo de
forma equivalente) → migración sin backfill y sin cambios para scoops
existentes.

**Alternativa considerada:** derivar la estrategia del tipo de scoop (p.ej.
`cronjob` → `recreate`).
**Descartada:** comportamiento implícito = sorpresa; el usuario debe poder
elegir por scoop.

### D2. Nombre del Deployment con sufijo de generación para blue-green

`build_deployment(scoop, ns, name_suffix=None, gen=None)`:
- `rolling`/`recreate`: nombre `<name>` (sin cambios).
- `blue_green`: nombre `<name>-v<gen>` donde `gen = _sanitize_label(version)`,
  truncado para cumplir 63 chars DNS-1123.
- `canary`: nombre `<name>-canary`.

El sufijo habilita la convivencia de generaciones. El selector del
Deployment agrega `laurel.io/gen=<sanitized(version)>` (immutable
post-creación: se fija al crear y nunca se edita). **Alternativa
considerada:** un solo Deployment y editar el selector.
**Descartada:** el selector es inmutable → el único switch viable es el del
Service.

### D3. Service estable `<name>` con selector parcheable (blue-green) o sin `gen` (canary)

- Blue-green: UN solo Service con nombre estable `<name>`, mismo
  port/targetPort, cuyo selector incluye `laurel.io/gen`. La conmutación es
  un PATCH atómico al selector (cambia `gen` de `vN` a `vN+1`): sin
  downtime, sin recrear Service, Ingress/DNS (`Domain → Service`)
  transparente.
- Canary: el selector del Service NO incluye `gen` → matchea ambas
  generaciones; un Service LoadBalancer balancea por pods Ready
  proporcional al conteo (no por peso), fracción de tráfico =
  `pods nuevos / total`, gateada por readinessProbe.

**Alternativa considerada:** canary por Ingress/route con annotations de
weight de traefik.
**Descartada:** rompe el contrato `Domain → Service` y añade dependencia del
Ingress controller.

### D4. `DeployService.deploy` como dispatcher por estrategia

Secuencias:
- `rolling` / `recreate` → bucle create/replace actual
  (`deploy.py:46-112`, incluye manejo de carrera 409 en `deploy.py:71-81`).
- `blue_green` → (1) create/replace Deployment `vN+1` (sin tráfico; el
  selector del Service aún apunta a `vN`) → (2) esperar `ready >= desired`
  (reusar lógica de status `deploy.py:179-193`) → (3) PATCH selector del
  Service a `gen` nuevo (switch) → (4) borrar `vN` (ground) o conservarlo
  para rollback.
- `canary` → escala el Deployment `<name>-canary`; promoción = replace del
  Deployment primario a la versión nueva + scale canary a 0.

`DeployRequest` (`controller.py`, `POST /api/scoops/<id>/deploy`) acepta
`{namespace, dry_run, strategy?, canary_replicas?}`; sin `strategy` se usa
`Scoop.deploy_strategy`.

### D5. Status en blue-green: resolver gen activo desde el Service

`status` hoy lee `read(scoop.name, ns)`. En blue-green el nombre del
Deployment ya no es `scoop.name`: se resuelve el gen activo leyendo el
selector del Service (fuente de verdad en el cluster) y se reportan **ambos**
Deployments (activo + reserva) en `pods`.

### D6. Cleanup de generaciones en undeploy y delete

Hoy `undeploy` borra por `scoop.name` y el delete del scoop
(`controller.py:205-253`) usa `exists("Deployment", ns, scoop.name)`. Con
nombres sufijados esto deja generaciones huérfanas: undeploy/delete deben
listar Deployments/HPA por label selector `app=<name>` y borrarlos todos.
**Alternativa considerada:** conservar siempre `vN` "ground".
**Descartada:** no, si el usuario hace undeploy quiere quitar todo.

### D7. HPA: solo el gen activo escala

`build_hpa` recibe el nombre dinámico del Deployment (o HPA por gen). En
blue-green solo el gen activo escala; el viejo se congela (scale a mínimo)
o se borra en el paso (4) de D4. En canary, el primario escala normal y el
canary escala según `canary_replicas`.

### D8. Orden de ship por fases

1. `recreate` + `rolling custom` (`maxSurge`/`maxUnavailable` desde
   `DEPLOY_MAX_SURGE`/`DEPLOY_MAX_UNAVAILABLE`): trivial (~10 líneas).
2. `blue_green`: sufijo de gen + espera-ready + PATCH de selector +
   cleanup (~40-60 líneas).
3. `canary`: solo si un flujo real lo pide (~30-50 líneas).

Cada fase se shipea y valida por separado; `rolling` sigue siendo el
default y ninguna fase toca el contrato `Domain → Service`.

### D9. Jenkins sin cambios

Jenkins solo hace build+push de imagen + `PUT` de version + `POST deploy`;
la estrategia vive en el scoop (`Scoop.deploy_strategy` o `strategy` en el
request del deploy). No se agrega configuración de pipeline.

## Risks / Trade-offs

- **Nombres únicos por versión:** Deployment/HPA con sufijo deben cumplir
  63 chars DNS-1123; el gen se trunca con `_sanitize_label`. Trade-off: un
  gen truncado puede colisionar para versiones largas muy similares
  (mitigado por el hash de la label `version` ya existente).
- **Cleanup de la generación vieja:** es responsabilidad nueva de deploy;
  hoy undeploy solo borra por `scoop.name` → hay que borrar generaciones
  residuales (D6). Riesgo de huérfanos si se omite.
- **Delete del scoop en blue-green:** el check de deploy activo consulta
  `exists("Deployment", ns, scoop.name)`; con nombres sufijados hay que
  listar por label `app=<name>` (D6).
- **HPA + blue-green:** solo el gen activo escala, el viejo se congela o se
  borra; un `min_replicas` alto en el gen viejo congelado dispararía
  autoscaling sobre un Deployment sin tráfico (mitigado: scale a 0 o borrar).
- **Selector inmutable del Deployment:** la label `gen` se fija al crear y
  nunca se edita; si se necesita cambiar de gen hay que recrear el
  Deployment (nunca editar en place).
- **Canary sin peso real:** el balanceo es proporcional al conteo de pods
  Ready, no por peso; suficiente para validar la nueva versión, no para
  percentages finos. Si se necesita weight → volver a evaluar Ingress/Service
  Mesh (fuera de scope hoy).
- **Rolling custom con valores inválidos:** `maxSurge`/`maxUnavailable`
  vienen de config; validar rangos (0-100%) para no romper el rollout.
