## Purpose

Recurso de primer nivel "Domain" que asocia un subdominio público (`host`)
a exactamente un `Scoop` (y por transitividad a su `Application`). Genera
Ingress + Certificate (LetsEncrypt) + DNS override cuando se deploya.

**Un dominio NO se autogenera al crear el scoop** — se crea como paso
separado cuando el usuario decide exponer el scoop. Esto permite que un
scoop `worker` o `cronjob` no tenga Ingress, y que dentro de una misma
app solo el scoop `api` elegido quede público.

Modelo:
```
App("notas")
├── Scoop("webapp")  ─── Domain("notas.resto.com")   ← expuesto
└── Scoop("mcp")     ─── (sin Domain)                ← interno
```

## ADDED Requirements

### Requirement: Domain CRUD

El sistema SHALL exponer una entidad `Domain` con los campos:
`id` (int, autoincrement), `application_id` (FK a `applications.id`,
requerido), `scoop_id` (FK a `scoops.id`, requerido),
`host` (string, único globalmente, formato `algo.dominio.tld`),
`tls` (bool, default `true`), `status`
(`pending|active|error`, default `pending`), `secret_name` (string,
default derivado de `host`: reemplazando `.` por `-`,
ej. `notas-resto-com`),
`created_at`, `updated_at`.

#### Scenario: Crear dominio para un scoop existente

- **WHEN** `POST /api/domains` con
  `{"application_id": 1, "scoop_id": 5, "host": "notas.resto.com"}`
  donde la app 1 existe, el scoop 5 existe, el scoop 5 pertenece a
  la app 1, y el scoop 5 es de tipo `api`
- **THEN** la API responde 201 con el body del `Domain`
- **AND** `status="pending"`, `secret_name="notas-resto-com"`
- **AND** se registra `audit_log` con acción `domain_create`

#### Scenario: Scoop no pertenece a la app

- **WHEN** `POST /api/domains` con `application_id=1` y `scoop_id=5`
  pero el scoop 5 tiene `application_id=2`
- **THEN** la API responde 400 con mensaje
  `Scoop 5 does not belong to application 1`

#### Scenario: Scoop no es de tipo api

- **WHEN** `POST /api/domains` con un scoop de tipo `worker` o `cronjob`
- **THEN** la API responde 400 con mensaje
  `Only api-type scoops can have a public domain`

#### Scenario: Host duplicado

- **WHEN** ya existe un `Domain` con `host="x.com"` y el cliente intenta
  crear otro con el mismo host
- **THEN** la API responde 409 con mensaje
  `Domain 'x.com' already exists`

#### Scenario: Host con formato inválido

- **WHEN** `host="notas resto.com"` (espacios) o `host="localhost"` o
  `host=""` o sin punto
- **THEN** la API responde 400 con detalle del error de validación
  (debe tener al menos un punto y cada label debe ser DNS-1123)

### Requirement: Domain deploy (creates Ingress + Certificate + DNS)

`POST /api/domains/<id>/deploy` SHALL crear/actualizar en el cluster:
1. `Ingress` con host y backend al `Service` del scoop referenciado
2. `Certificate` LetsEncrypt (`cert-manager.io/v1`) con `dnsNames=[host]`
3. Override DNS en el ConfigMap `kube-system/coredns-custom` para que el
   subdominio resuelva a `DNS_OVERRIDE_LAN_IP` desde dentro del cluster
   (necesario para HTTP-01 self-check de cert-manager)

#### Scenario: Primer deploy de un dominio

- **WHEN** `POST /api/domains/42/deploy` y el dominio está en `pending`
- **THEN** la API aplica los 3 recursos en el namespace del scoop
- **AND** devuelve `{"host": "notas.resto.com", "namespace": "notas",
  "resources": [{kind: "Ingress", action: "created"}, ...], "dns_override": "added"}`
- **AND** el `Domain.status` pasa a `pending` (cert aún sin emitir)
- **AND** se registra `audit_log` con acción `domain_deploy`

#### Scenario: Re-deploy (idempotente)

- **WHEN** `POST /api/domains/42/deploy` por segunda vez
- **THEN** los recursos se parchean (no se recrean)

### Requirement: Domain undeploy (removes Ingress + Certificate + DNS)

`DELETE /api/domains/<id>/deploy` SHALL eliminar los 3 recursos del
cluster en orden inverso al deploy (Certificate → Ingress → DNS override).
El registro `Domain` en BD se conserva; solo se cambia `status` a
`pending` (sin recursos aplicados).

#### Scenario: Undeploy limpio

- **WHEN** `DELETE /api/domains/42/deploy` con 3 recursos aplicados
- **THEN** Certificate, Ingress y entrada DNS se eliminan
- **AND** `Domain.status` pasa a `pending`
- **AND** el scoop asociado sigue activo (no se ve afectado)

### Requirement: Domain status (contrasta BD con cluster)

`GET /api/domains/<id>/status` SHALL devolver el estado de los recursos
del dominio contrastado con el cluster.

#### Scenario: Cert emitido

- **WHEN** el Certificate tiene condición `Ready=True`
- **THEN** la respuesta incluye `"certificate_ready": true`,
  `"domain_status": "active"`
- **AND** `Domain.status` en BD se promueve a `active`

#### Scenario: Cert en proceso

- **WHEN** el Certificate no tiene condición `Ready=True` pero existe
- **THEN** `domain_status: "pending"`, `certificate_ready: false`,
  se incluyen challenges activos

#### Scenario: Cert con error

- **WHEN** el último challenge tiene `state=invalid`
- **THEN** `domain_status: "error"`, `Domain.status` en BD se pone
  a `error` y se registra `audit_log`

### Requirement: Domain certificate status + logs

`GET /api/domains/<id>/certificate` SHALL devolver el estado detallado
del Certificate (condiciones, challenges, último CertificateRequest,
eventos).

`GET /api/domains/<id>/certificate/logs` SHALL devolver logs filtrados
del controller `cert-manager` para el certificado del dominio.

#### Scenario: Logs del cert-manager

- **WHEN** `GET /api/domains/42/certificate/logs?tail=100`
- **THEN** la API devuelve entradas de logs de los pods de
  `cert-manager` que mencionan el `secret_name` del dominio

### Requirement: Domain delete soft + cascade on app force-delete

`DELETE /api/domains/<id>` SHALL soft-delete el dominio
(`deleted_at = now()`), ocultándolo del listado. Si el dominio tiene
recursos aplicados, también se hace undeploy antes.

#### Scenario: Delete con recursos aplicados

- **WHEN** `DELETE /api/domains/42` con Ingress+Cert+DNS aplicados
- **THEN** primero se hace undeploy (recursos cluster eliminados)
- **THEN** el registro se marca `deleted_at`
- **AND** el scoop asociado sigue activo

#### Scenario: Force-delete de la app dueña

- **WHEN** `DELETE /api/apps/<id>?force=true` y la app tiene 2 domains
- **THEN** ambos domains se eliminan en cascada antes de borrar el
  namespace (cada uno: undeploy + soft-delete)
- **AND** se registran 2 `audit_log` con acción `domain_force_delete`

### Requirement: Domain NO autogenerado al crear App o Scoop

#### Scenario: Crear app no crea dominio

- **WHEN** `POST /api/apps` con name válido
- **THEN** NO se crea ningún `Domain`
- **AND** la app no tiene `default_host` ni similar

#### Scenario: Crear scoop no crea dominio

- **WHEN** `POST /api/scoops` con type=api
- **THEN** NO se crea ningún `Domain`
- **AND** el scoop no tiene `host` ni `url` autogenerados

### Requirement: Listado de dominios por app

`GET /api/domains?application_id=<id>` SHALL devolver la lista de
domains de una app específica (sin soft-deleted).

#### Scenario: Filtrar por app

- **WHEN** `GET /api/domains?application_id=1`
- **THEN** la respuesta solo incluye domains con `application_id=1`
  y `deleted_at IS NULL`

#### Scenario: Listar todos

- **WHEN** `GET /api/domains?page=1&limit=20`
- **THEN** devuelve paginación de todos los domains activos