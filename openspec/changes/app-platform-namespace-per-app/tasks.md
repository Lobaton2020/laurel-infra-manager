# Tasks: app-platform-namespace-per-app

## Phase 1: Lead (modelo + migración + refactor Domain)

### 1.1. Crear módulo `apps/`
- [ ] Crear `app/modules/apps/__init__.py` (blueprint export)
- [ ] Crear `app/modules/apps/model.py` con clase `Application`:
  `id, name, slug, description, github_repo_url, docker_image_base,
  created_at, updated_at, deleted_at` + `ix_applications_slug` único
- [ ] Crear `app/modules/apps/schema.py` con Pydantic:
  `ApplicationCreate, ApplicationUpdate, ApplicationResponse,
  ApplicationListResponse` (validar `name` 1-100, `slug` derivado
  de `name` con `slugify()`, `description` <= 500, `docker_image_base`
  regex simple)
- [ ] Crear `app/modules/apps/service.py` con `AppsService`:
  `create, get, list, update, archive, force_delete` (force_delete
  usa `K8sService` y `ScoopService.archive_for_app`)
- [ ] Crear `app/modules/apps/controller.py` con blueprint
  `bp = Blueprint("apps", __name__, url_prefix="/api/apps")` +
  endpoints `GET "", GET <id>, POST "", PUT <id>, DELETE <id>`,
  `DELETE <id>?force=true`

### 1.2. Registrar blueprint en `app/__init__.py`
- [ ] Importar `apps_bp` y registrarlo en `create_app()` con
  `app.register_blueprint(apps_bp)`
- [ ] Agregar entrada SWAGGER `"Apps"`

### 1.3. Migración BD + backfill
- [ ] Crear `migrations/2026_08_17_apps.sql` (o agregar al `migrate.py`)
  con:
  ```sql
  CREATE TABLE applications (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    slug VARCHAR(63) NOT NULL,
    description VARCHAR(500),
    github_repo_url VARCHAR(255),
    docker_image_base VARCHAR(255),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME,
    UNIQUE KEY ix_applications_slug (slug),
    UNIQUE KEY ix_applications_name (name)
  );
  ALTER TABLE scoops ADD COLUMN application_id INT,
    ADD INDEX ix_scoops_application_id (application_id);
  ```
- [ ] Backfill script: `python migrate.py --backfill-apps`:
  - Agrupa `scoops.application` distintos
  - Para cada uno, `slug = slugify(application)`
  - Valida unicidad de slugs (falla explícito si colisión)
  - INSERT en `applications` con `name = application` original
  - UPDATE `scoops.application_id` por match
- [ ] Documentar en `migrate.py` cómo correr el backfill

### 1.4. Refactor `Scoop` model
- [ ] Agregar `application_id = db.Column(db.Integer,
  db.ForeignKey("applications.id"), nullable=True,
  index=True)` en `app/modules/scoops/model.py`
- [ ] Agregar `application = db.relationship("Application",
  backref=db.backref("scoops", lazy="dynamic"))`

### 1.5. Refactor `ManifestService.namespace_for`
- [ ] Modificar `app/modules/scoops/manifest.py` para implementar
  la prioridad: override > scoop.namespace > application.slug >
  DEFAULT_NAMESPACE
- [ ] Test unitario en `tests/test_manifests.py` con cada path

### 1.6. Refactor `Scoop` schema + service
- [ ] `ScoopBase` agrega `application_id: Optional[int]`
- [ ] `ScoopResponse.from_scoop` incluye `application_id` y
  `application_slug` (computed)
- [ ] `ScoopService.create` y `update` validan que
  `application_id` referencia una `Application` existente (404 si no)
- [ ] Agregar `ScoopService.archive_for_app(app_id)` que pone
  `status="archived"` en todos los scoops con `application_id`

### 1.7. Tests del modelo + service
- [ ] `tests/test_apps.py` (nuevo):
  - Crear app con name válido → 201 + slug derivado
  - Crear app con name duplicado → 409
  - Listar apps (solo no-deleted)
  - Update app (slug inmutable)
  - Force-delete con scoops activos (mockear K8s)
  - Force-delete con pods stuck (mockear K8s con timeout)
  - Backfill: agrupar scoops por application, crear apps

### 1.8. Crear módulo `domains/` + refactor ManifestService
- [ ] Refactor `app/modules/scoops/manifest.py`:
  - Quitar `build_ingress`, `build_certificate`, `ingress_host`
  - `ManifestService.build()` solo emite Deployment/Service/CronJob/HPA
- [ ] Refactor `app/modules/scoops/deploy.py`:
  - Quitar el bloque de DNS override (líneas con `ClusterDNSService.add/remove`)
  - Quitar los campos `host`, `dns_override`, `manual_hosts_lines` del response
- [ ] Refactor `app/modules/scoops/schema.py`:
  - Quitar `host`, `url`, `status_label` computed fields de `ScoopResponse`
  - Quitar `Ingress`, `Certificate` references en la respuesta
- [ ] Crear `app/modules/domains/__init__.py` (blueprint export)
- [ ] Crear `app/modules/domains/model.py` con clase `Domain`:
  `id, application_id, scoop_id, host (unique), tls, status, secret_name,
  created_at, updated_at, deleted_at`. FKs con índices.
- [ ] Crear `app/modules/domains/schema.py` con Pydantic:
  `DomainCreate, DomainUpdate, DomainResponse, DomainListResponse,
  DomainStatusResponse`. Validar `host` con regex
  `^([a-z0-9]([-a-z0-9]*[a-z0-9])?\.)+[a-z]{2,}$`,
  validar `scoop.application_id == application_id` (400 si no),
  validar `scoop.type == 'api'` (400 si no).
- [ ] Crear `app/modules/domains/service.py` con `DomainService`:
  - `create, get, list, update, soft_delete`
  - `deploy(domain)` → build Ingress + Certificate + DNS override,
    aplicar via `K8sService` (idempotente), audit_log
  - `undeploy(domain)` → eliminar Ingress → Certificate → DNS override
  - `status(domain)` → contrastar BD con cluster, promote status
    (pending → active → error)
  - `certificate_status(domain)` → reusar lógica del scoop pero
    parametrizada por domain
  - `certificate_logs(domain)` → idem
- [ ] Crear `app/modules/domains/controller.py` con blueprint
  `bp = Blueprint("domains", __name__, url_prefix="/api/domains")`
  + endpoints CRUD + `POST/DELETE /api/domains/<id>/deploy`,
  `GET /api/domains/<id>/status`,
  `GET /api/domains/<id>/certificate`,
  `GET /api/domains/<id>/certificate/logs`
- [ ] Registrar `domains_bp` en `app/__init__.py` + entrada SWAGGER
  "Domains"
- [ ] Migración SQL para `domains`:
  ```sql
  CREATE TABLE domains (
    id INT PRIMARY KEY AUTO_INCREMENT,
    application_id INT NOT NULL,
    scoop_id INT NOT NULL,
    host VARCHAR(253) NOT NULL,
    tls TINYINT(1) NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    secret_name VARCHAR(63) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    deleted_at DATETIME,
    UNIQUE KEY ix_domains_host (host),
    INDEX ix_domains_application_id (application_id),
    INDEX ix_domains_scoop_id (scoop_id),
    FOREIGN KEY (application_id) REFERENCES applications(id),
    FOREIGN KEY (scoop_id) REFERENCES scoops(id)
  );
  ```
- [ ] Migración: por cada scoop existente con `url_registry` apuntando
  a una imagen que tiene un subdominio público (heurística: scoop
  tipo `api` con `port` asignado), crear un `Domain` retrospectivo
  con `host = <scoop.name>.<INGRESS_BASE_DOMAIN>`. Esto preserva los
  Ingress/Certificates existentes en el cluster. Log explícito de
  cuántos domains se crearon retroactivamente.
- [ ] Actualizar `apps/AppsService.force_delete`: paso 0 = undeploy
  de cada domain de la app
- [ ] Tests `tests/test_domains.py`:
  - Crear domain OK
  - Scoop no pertenece a la app → 400
  - Scoop worker → 400
  - Host duplicado → 409
  - Host inválido → 400
  - Deploy aplica 3 recursos en orden
  - Undeploy elimina 3 recursos en orden inverso
  - Status promotion (pending → active cuando cert Ready)
  - Status error cuando challenge invalid
  - Soft-delete con undeploy previo
  - Force-delete app elimina sus domains en cascada
- [ ] Actualizar tests existentes que dependan de `build_certificate` /
  `Ingress` / `host` en `tests/test_manifests.py` y `tests/test_scoops.py`

## Phase 2: Equipos en paralelo

### 2.1. GitHub integration (lead o `build:github-runner`)
- [ ] Crear `app/modules/integrations/__init__.py`
- [ ] Crear `app/modules/integrations/github/__init__.py` (exporta
  `GitHubService`)
- [ ] Crear `app/modules/integrations/github/service.py` con
  `GitHubService`:
  - Lee PAT de `system_secret("github_pat")`
  - `create_empty_repo(name, private=False) → dict`
  - `repo_exists(owner, name) → bool`
  - Usa `requests` directo contra `https://api.github.com`
  - Mapea errores: 401→502, 422 name_already_exists→409,
    network→503
- [ ] Tests `tests/test_github_integration.py` con
  `requests_mock` o `responses`:
  - PAT no configurado → 503
  - Crear repo OK
  - Repo ya existe → 409
  - 401 → 502
  - repo_exists: 200→True, 404→False, network→False

### 2.2. Docker Hub integration (lead o `build:docker-runner`)
- [ ] Crear `app/modules/integrations/docker/__init__.py`
- [ ] Crear `app/modules/integrations/docker/service.py` con
  `DockerHubService`:
  - Lee PAT de `system_secret("docker_pat")`
  - `validate_image_ref(image_ref) → bool`
  - `image_exists(image_ref) → Optional[bool]` (None si no se
    puede verificar)
  - Usa `requests` directo contra
    `https://hub.docker.com/v2/repositories/<org>/<repo>/tags/<tag>/`
    (timeout 5s)
- [ ] Tests `tests/test_docker_integration.py`:
  - PAT no configurado → None
  - Formato válido → True
  - Formato inválido → False
  - Imagen existe → True
  - Imagen 404 → False
  - Timeout → None

### 2.3. System secrets: agregar github_pat + docker_pat
- [ ] En `app/modules/system/service.py`, agregar a la whitelist
  `MANAGED`:
  ```python
  ManagedSecret("github_pat", "laurel-infra-manager",
                "laurel-github", "pat", "text", "laurel-infra-manager"),
  ManagedSecret("docker_pat", "laurel-infra-manager",
                "laurel-docker", "pat", "text", "laurel-infra-manager"),
  ```
- [ ] Tests: listar 4 secretos (los 2 nuevos aparecen)
- [ ] Tests: PUT github_pat con content vacío → 400

### 2.4. Wiring de integrations en `AppsService.create`
- [ ] Si `ApplicationCreate.create_github_repo=True`, llamar
  `GitHubService.create_empty_repo(slug, private=False)` antes de
  insertar la app
- [ ] Si el PAT no está configurado, skip + audit_log con
  `github_repo_skipped`
- [ ] Si el repo ya existe (409), no insertar la app y propagar
  error
- [ ] Tests en `tests/test_apps.py` con mock de GitHubService

### 2.5. ConfigStore: validar app contra Application
- [ ] En `app/modules/configstore/service.py`, `create_config_map`
  y `create_secret`: si el body trae `app`, validar que
  `Application.slug == app` (404 si no)
- [ ] Agregar FK opcional `application_id` en `ConfigMap` y
  `Secret` (tablas en BD)
- [ ] En `force_delete(app_id)`: antes de borrar namespace,
  eliminar ConfigMaps/Secrets del namespace con label selector
  `app=<slug>`
- [ ] Tests en `tests/test_configstore.py`

### 2.6. Frontend (configurator-lob)
- [ ] Crear `src/api/apps.ts` con `appsApi.{list, get, create,
  update, forceDelete}` + tipos `Application, ApplicationCreate`
- [ ] Crear `src/pages/Apps.tsx` con:
  - Tabla de apps con columnas: name, slug, scoops_count,
    github_repo_url (link), docker_image_base, created_at
  - Botón "Nueva app" → form modal
  - Form: name, description, docker_image_base (con validación
    opcional vía `dockerApi.image_exists`), toggle "Crear repo en
    GitHub" (deshabilitado si no hay PAT configurado)
  - Botón "Eliminar" con confirmación + segundo confirm para
    `force=true`
- [ ] Modificar `src/pages/ScoopNew.tsx`:
  - Si hay apps (`GET /api/apps`), mostrar `<select>` en lugar
    de input libre para `application`
  - Si se selecciona, autocompletar y bloquear `application` y
    `namespace` (mostrar `<input disabled>` con valor del slug)
  - Si no hay apps, fallback a input libre (legacy)
- [ ] Modificar `src/components/Layout.tsx`: agregar link "Apps"
  en sidebar (con icono `AppWindow` o similar)
- [ ] `tsc -b` verde
- [ ] Crear `src/api/domains.ts` con `domainsApi.{list, get, create,
  update, delete, deploy, undeploy, status, certificate, certificateLogs}`
  + tipos `Domain, DomainCreate, DomainStatus`
- [ ] Crear `src/pages/Domains.tsx` con:
  - Tabla de domains con columnas: host, application (nombre),
    scoop (nombre), tls, status, certificate_ready
  - Botón "Nuevo dominio" → form con selects: application,
    scoop (filtrado por app seleccionada y tipo `api`),
    host, tls checkbox
  - Botones "Deploy" / "Undeploy" / "Eliminar" por fila
- [ ] Crear `src/pages/DomainDetail.tsx` con:
  - Info del domain (host, status, application, scoop, namespace)
  - Sección "Certificado TLS" con estado del Certificate (condiciones,
    challenges activos, último CertificateRequest, eventos) — la
    misma UI que tenía `ScoopDetail` pero apuntando al domain
  - Sección "Logs de cert-manager" (filtrados por el secret_name
    del domain)
- [ ] Modificar `src/pages/ScoopDetail.tsx`:
  - Quitar la sección de certificado (ahora vive en DomainDetail)
  - Mostrar link "Ver dominios asociados" → `GET /api/domains?scoop_id=<id>`
- [ ] `tsc -b` verde

## Phase 3: Lead (verificación + integración)

### 3.1. Ejecutar migrations
- [ ] Aplicar migración SQL en BD dev (`192.168.20.240/orchestrator`)
- [ ] Correr `python migrate.py --backfill-apps`
- [ ] Verificar en BD: tabla `applications` poblada, `scoops`
  con `application_id` set

### 3.2. Tests E2E locales
- [ ] Reiniciar gunicorn local
- [ ] `pytest tests/test_apps.py tests/test_github_integration.py
  tests/test_docker_integration.py -v` → todo verde
- [ ] `pytest tests/test_scoops.py tests/test_configstore.py
  tests/test_system_secrets.py -v` → todo verde (no romper lo
  existente)
- [ ] Curl E2E:
  - `POST /api/apps` con name válido → 201
  - `GET /api/apps/<id>` → 200 con scoops_count
  - `POST /api/scoops` con `application_id=<id>` → 201
  - `POST /api/scoops/<id>/deploy` → 200, recursos en
    namespace del slug
  - `DELETE /api/apps/<id>?force=true` → 200, namespace borrado,
    scoop archived

### 3.3. Build frontend
- [ ] `npm run build -- --outDir dist-new` → exit 0
- [ ] Verificar bundle dist-new contiene las páginas nuevas

### 3.4. Commit + push
- [ ] Commit backend con mensaje
  `feat: applications with namespace-per-app + github/docker integrations`
- [ ] Commit frontend con mensaje
  `feat: apps page + scoop app selector`
- [ ] Push por el usuario (requiere credenciales)

## Phase 4: Archive

### 4.1. OpenSpec archive
- [ ] `/opsx-archive app-platform-namespace-per-app` (mergea
  delta specs a main specs)