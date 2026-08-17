## Purpose

Modelo de primer nivel "Application" que agrupa N scoops bajo un namespace
dedicado en el cluster, con metadata externa (repo GitHub, imagen Docker base)
y lifecycle propio (creación idempotente, borrado en cascada con confirmación).

## ADDED Requirements

### Requirement: Application CRUD

El sistema SHALL exponer una entidad `Application` con los campos:
`id` (int, autoincrement), `name` (string, único, 1-100 chars),
`slug` (string DNS-1123, único, derivado de `name`),
`description` (string opcional, hasta 500 chars),
`github_repo_url` (URL opcional), `docker_image_base` (string opcional,
formato `registry/org/name`), `created_at` (datetime), `updated_at`
(datetime), `deleted_at` (nullable, soft-delete flag).

#### Scenario: Crear una app nueva

- **WHEN** el cliente envía `POST /api/apps` con `name="Mi App"` y
  `description="Backend principal"`
- **THEN** la API responde 201 con el body de la `Application`
- **AND** `slug` se calcula como `mi-app`
- **AND** se registra `audit_log` con acción `app_create`

#### Scenario: Nombre con caracteres no-DNS

- **WHEN** el cliente envía `POST /api/apps` con `name="!!!@@@"`
- **THEN** la API responde 400 con detalle del error de validación DNS-1123

#### Scenario: Nombre duplicado

- **WHEN** ya existe una app con `name="Mi App"` y el cliente intenta crear
  otra con el mismo nombre
- **THEN** la API responde 409 con mensaje `Application name already exists`

### Requirement: Application list and detail

#### Scenario: Listar apps

- **WHEN** el cliente envía `GET /api/apps?page=1&limit=20`
- **THEN** la API responde 200 con `items` (lista de apps), `total`,
  `page`, `limit`, `pages`
- **AND** el listado incluye solo apps con `deleted_at IS NULL`

#### Scenario: Detalle de una app

- **WHEN** el cliente envía `GET /api/apps/<id>` para una app existente
- **THEN** la API responde 200 con el body completo de la app
- **AND** incluye `scoops_count` (número de scoops vivos con
  `application_id=<id>`) y `namespace` (= `slug`)

### Requirement: Update application metadata

#### Scenario: Actualizar descripción y repo

- **WHEN** el cliente envía `PUT /api/apps/<id>` con `description="Nuevo"`
  y `github_repo_url="https://github.com/owner/mi-app"`
- **THEN** la API responde 200 con el body actualizado
- **AND** el `slug` y `name` NO cambian (son inmutables)

### Requirement: Application namespace lifecycle

Una `Application` SHALL estar asociada a un namespace dedicado con nombre
igual a su `slug`. El namespace se crea de forma idempotente la primera vez
que se despliega un scoop de la app; la API SHALL encargarse de crearlo si
no existe.

#### Scenario: Deploy de scoop con application_id crea el namespace

- **WHEN** se hace `POST /api/scoops/<id>/deploy` sobre un scoop con
  `application_id=<X>` y la app `X` tiene `slug="mi-app"`
- **THEN** antes de aplicar manifiestos, el sistema verifica si el namespace
  `mi-app` existe; si no, lo crea
- **AND** todos los recursos del scoop se despliegan en `mi-app`

#### Scenario: Namespace ya existe

- **WHEN** el namespace `mi-app` ya existe en el cluster
- **THEN** el deploy continúa sin recrearlo (idempotente)

### Requirement: Application force delete with cascade

`DELETE /api/apps/<id>?force=true` SHALL eliminar en cascada todos los
recursos del namespace asociado a la app (Deployment, Service, Ingress,
Certificate, ConfigMap, Secret) y el namespace mismo, y marcar los scoops
huérfanos como `status="archived"`.

#### Scenario: Force delete con scoops activos

- **WHEN** el cliente envía `DELETE /api/apps/<id>?force=true` para una app
  con 3 scoops activos y un namespace con recursos vivos
- **THEN** la API responde 200 con un body `{"namespace": "mi-app",
  "resources": [...lista de recursos borrados...], "scoops_archived": 3}`
- **AND** todos los recursos del namespace están eliminados en el cluster
- **AND** los scoops en BD tienen `status="archived"` y la app tiene
  `deleted_at` set
- **AND** se registra `audit_log` con acción `app_force_delete` y el detalle
  de recursos borrados

#### Scenario: Force delete con pods stuck en Terminating

- **WHEN** algún recurso del namespace no se elimina en 30 segundos
- **THEN** la API responde 207 (Multi-Status) con el detalle de lo que se
  borró y lo que quedó pendiente; los scoops pasan a `status="error"`
- **AND** se registra `audit_log` con acción `app_force_delete_partial`

#### Scenario: Delete sin force

- **WHEN** el cliente envía `DELETE /api/apps/<id>` (sin `force=true`)
- **THEN** la API responde 400 con mensaje `?force=true required to delete
  an application with active scoops`

### Requirement: Application sin application_id (backwards compat)

#### Scenario: Crear scoop sin application_id

- **WHEN** el cliente crea un scoop con `application="legacy-app"` y no
  envía `application_id`
- **THEN** el scoop se crea con `application_id=NULL`
- **AND** su `namespace` por defecto es `user-apps` (comportamiento legacy)

#### Scenario: Backfill automático

- **WHEN** se ejecuta la migración que crea la tabla `applications`
- **THEN** para cada `application` distinta en `scoops` se crea una
  `Application` con `slug` derivado del nombre
- **AND** se setea `scoops.application_id` al id de la app creada
- **AND** si dos apps existentes sluggean al mismo valor, la migración
  falla con error explícito (no se aplica)

## ADDED Requirements

### Requirement: GitHub integration bootstrap on create

`POST /api/apps` SHALL aceptar el flag opcional `create_github_repo: bool`.
Si es `true` y el secret `github_pat` está configurado, el sistema SHALL
crear un repo vacío en GitHub con nombre igual al `slug` de la app y
almacenar la URL resultante en `github_repo_url`. Si el flag es `false`
o el PAT no está configurado, no se intenta la llamada externa.

#### Scenario: Crear app con create_github_repo=true y PAT configurado

- **WHEN** `POST /api/apps` con `name="X"`, `create_github_repo=true` y
  el secret `github_pat` configurado
- **THEN** la API llama a `GitHubService.create_empty_repo("x", private=False)`
- **AND** la respuesta incluye `github_repo_url="https://github.com/<owner>/x"`
- **AND** se registra `audit_log` con acción `github_repo_created`

#### Scenario: Crear app sin PAT

- **WHEN** `POST /api/apps` con `create_github_repo=true` y el secret
  `github_pat` no configurado
- **THEN** la app se crea igual, pero `github_repo_url=null`
- **AND** se registra `audit_log` con acción `app_create` y
  `details={"github_repo_skipped": "pat_missing"}`

#### Scenario: Repo ya existe en GitHub

- **WHEN** ya existe un repo con ese nombre en la cuenta del PAT
- **THEN** la API responde 409 con mensaje `GitHub repo already exists`
  y la app NO se crea en BD

### Requirement: Docker Hub integration metadata

`POST /api/apps` SHALL aceptar el campo opcional `docker_image_base`
(formato `registry/org/name`). Si está presente, el sistema SHALL validar
la sintaxis y opcionalmente (si `docker_pat` está configurado) que la
imagen exista en Docker Hub.

#### Scenario: Crear app con docker_image_base válido

- **WHEN** `POST /api/apps` con `docker_image_base="aflobaton/mi-app"`
- **THEN** la API valida el formato y responde 201 con el body de la app

#### Scenario: Formato inválido

- **WHEN** `docker_image_base="!!!@@@"` o vacío
- **THEN** la API responde 400 con detalle del error de validación