## Purpose

Soporte en Laurel-infra-manager para estrategias de deploy seleccionables
por scoop (`rolling` default, `recreate`, `blue_green`, `canary`), con un
contrato estable `Domain → Service → Deployment` y sin cambios en Jenkins.

## ADDED Requirements

### Requirement: Deploy strategy selection

El campo `Scoop.deploy_strategy` SHALL ser un enum con valores
`rolling|blue_green|canary|recreate` y default `"rolling"`. El endpoint
`POST /api/scoops/<id>/deploy` SHALL aceptar `strategy` y `canary_replicas`
opcionales; sin `strategy` en el request SHALL usar `Scoop.deploy_strategy`.

#### Scenario: Deploy con strategy explícita

- **WHEN** `POST /api/scoops/<id>/deploy` con `{"strategy": "recreate"}`
- **THEN** el Deployment del scoop se crea con
  `spec.strategy.type == "Recreate"`

#### Scenario: Deploy sin strategy usa el default del scoop

- **WHEN** `POST /api/scoops/<id>/deploy` sin `strategy` y el scoop tiene
  `deploy_strategy="blue_green"`
- **THEN** el deploy se ejecuta con blue-green

#### Scenario: Default rolling

- **WHEN** un scoop se crea sin `deploy_strategy`
- **THEN** `scoop.deploy_strategy == "rolling"`
- **AND** el comportamiento de deploy es el actual (RollingUpdate de K8s)

### Requirement: Recreate y rolling custom

`build_deployment` SHALL emitir `spec.strategy` para `recreate` y para
`rolling` con overrides de `maxSurge`/`maxUnavailable` (desde config
`DEPLOY_MAX_SURGE`/`DEPLOY_MAX_UNAVAILABLE`).

#### Scenario: Recreate

- **WHEN** un scoop con `deploy_strategy="recreate"` se deploya
- **THEN** el Deployment lleva `spec.strategy = {"type": "Recreate"}`
- **AND** el rollout provoca downtime deliberado (borra los pods viejos
  antes de crear los nuevos)

#### Scenario: Rolling custom

- **WHEN** un scoop con `deploy_strategy="rolling"` y config
  `DEPLOY_MAX_SURGE=1`, `DEPLOY_MAX_UNAVAILABLE=0` se deploya
- **THEN** el Deployment lleva
  `spec.strategy = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}`

### Requirement: Blue-green deploy

En blue-green SHALL convivir UN Service estable `<name>` con selector
parcheable y dos Deployments `<name>-v<gen>`/`<name>-v<gen+1>`. El switch
SHALL ser un PATCH atómico del selector del Service (sin downtime). El
Deployment SHALL llevar la label inmutable `laurel.io/gen=<version>` en el
selector y el template (el selector de un Deployment es inmutable
post-creación, nunca se edita).

#### Scenario: Switch blue-green

- **WHEN** se deploya una versión nueva en un scoop blue-green
- **THEN** se crea el Deployment `<name>-v<gen_nuevo>` sin tráfico
- **AND** el deploy espera a que `ready >= desired` del gen nuevo
- **AND** el selector del Service `<name>` se parchea al gen nuevo
- **AND** el gen anterior se borra (o se conserva para rollback)
- **AND** en ningún momento el Service queda sin selector válido

#### Scenario: Nombres con sufijo de generación

- **WHEN** un scoop blue-green con `version="1.2.3"` se deploya
- **THEN** el Deployment se llama `<name>-v1_2_3`
- **AND** el nombre cumple 63 chars DNS-1123 (gen truncado con
  `_sanitize_label`)

### Requirement: Canary deploy por proporción de réplicas

El Service del scoop en canary SHALL NO incluir la label `gen` en su
selector (matchea ambas generaciones). El balanceo SHALL ser proporcional
al conteo de pods Ready (no por peso), gateado por readinessProbe. La
promoción SHALL ser un replace del Deployment primario a la versión nueva
más scale del `<name>-canary` a 0.

#### Scenario: Canary recibe fracción del tráfico

- **WHEN** hay `N` pods del Deployment primario y `M` pods del
  Deployment `<name>-canary` listos
- **THEN** la fracción de tráfico hacia la nueva versión es
  `M / (N + M)`

#### Scenario: Promoción canary

- **WHEN** el canary se promueve
- **THEN** el Deployment primario se actualiza a la versión nueva
- **AND** el Deployment `<name>-canary` se escala a 0

### Requirement: HPA dinámico

`build_hpa` SHALL apuntar al Deployment activo (o crear HPA por gen). En
blue-green SOLO el gen activo escala; el gen viejo se congela o se borra.

#### Scenario: HPA apunta al gen activo

- **WHEN** el switch blue-green pasa el tráfico al gen nuevo
- **THEN** el HPA escala el Deployment del gen nuevo
- **AND** el HPA no escala el Deployment del gen anterior

### Requirement: Status blue-green

`status` SHALL resolver el gen activo leyendo el selector del Service
(fuente de verdad en el cluster) y SHALL reportar ambos Deployments
(activo + reserva) en `pods`.

#### Scenario: Status con dos generaciones

- **WHEN** un scoop blue-green tiene los Deployments `<name>-v1` (activo)
  y `<name>-v2` (reserva)
- **THEN** `GET /api/scoops/<id>/status` reporta el Deployment activo y el
  de reserva en `pods`, con el activo identificado

### Requirement: Cleanup de generaciones

`undeploy` y el delete del scoop SHALL eliminar todos los Deployments/HPA
del scoop listando por label selector `app=<name>` (no solo por
`scoop.name`), para no dejar generaciones huérfanas.

#### Scenario: Undeploy limpia generaciones residuales

- **WHEN** se hace undeploy de un scoop blue-green con Deployments
  `<name>-v1` y `<name>-v2`
- **THEN** ambos Deployments (y sus HPA) se eliminan

#### Scenario: Delete del scoop detecta deploy activo sufijado

- **WHEN** el delete del scoop chequea si hay deploy activo en un scoop
  blue-green
- **THEN** el check lista por label `app=<name>` en vez de consultar
  `exists("Deployment", ns, scoop.name)`

### Requirement: Contrato estable con Domain

El Service del scoop SHALL conservar el nombre `<name>` y puerto
estable en todas las estrategias → Ingress/DNS (`Domain → Service`) no
cambian. El canary por Ingress (weight) queda fuera de scope: rompe el
contrato `Domain → Service`.

#### Scenario: Domain sigue apuntando al Service estable

- **WHEN** un scoop pasa de `rolling` a `blue_green` o `canary`
- **THEN** el Service conserva el nombre `<name>` y el mismo
  port/targetPort
- **AND** el recurso `Domain` del scoop no necesita cambios

### Requirement: Jenkins sin cambios

Jenkins SHALL seguir solo build+push de imagen + `PUT` de version +
`POST deploy`; la estrategia SHALL vivir en el scoop
(`Scoop.deploy_strategy` o `strategy` en el request del deploy).

#### Scenario: Pipeline existente sigue funcionando

- **WHEN** Jenkins hace `POST /api/scoops/<id>/deploy` sin `strategy`
- **THEN** el deploy usa `Scoop.deploy_strategy` del scoop
- **AND** no se requiere configuración nueva en el pipeline
