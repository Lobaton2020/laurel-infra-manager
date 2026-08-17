## Purpose

Cliente PAT-based para GitHub que permite crear repositorios vacíos y
verificar existencia desde la plataforma. Todos los repos se crean en
la organización `laurel-applications` con prefijo `laurel_` en el
nombre para identificarlos como apps administradas por la plataforma.

No incluye OAuth, webhooks ni operaciones de push: solo bootstrap mínimo.

## ADDED Requirements

### Requirement: GitHub PAT authentication

El sistema SHALL autenticar todas las llamadas a la API de GitHub usando
un Personal Access Token (PAT) almacenado en el secret del sistema
`github_pat`. El token SHALL tener scopes `repo` + `admin:org` (necesario
para crear repos en una organización a nombre del bot).

#### Scenario: PAT no configurado

- **WHEN** cualquier operación de GitHub se invoca y el secret `github_pat`
  no está configurado o está vacío
- **THEN** el servicio lanza `AppError` con `status_code=503` y mensaje
  `GitHub PAT not configured; configure it in /api/system/secrets/github_pat`

#### Scenario: PAT inválido

- **WHEN** la API de GitHub responde 401
- **THEN** el servicio lanza `AppError` con `status_code=502` y mensaje
  `GitHub authentication failed`

### Requirement: GitHub repo naming convention

Todos los repos creados por la plataforma SHALL cumplir `laurel_<slug>`
donde `<slug>` es el slug DNS-1123 de la `Application`. El nombre
resultante SHALL tener como máximo 100 caracteres (límite de GitHub).

#### Scenario: Nombre generado a partir del slug

- **WHEN** se crea una `Application` con `name="Mi App"` (slug `mi-app`)
  y `create_github_repo=true`
- **THEN** el nombre del repo en GitHub es `laurel_mi-app`
- **AND** el repo pertenece a la organización `laurel-applications`
- **AND** la URL resultante es
  `https://github.com/laurel-applications/laurel_mi-app`

#### Scenario: Slug demasiado largo

- **WHEN** `name` genera un slug > 100 chars después de aplicar el prefijo
- **THEN** la API responde 400 con mensaje
  `GitHub repo name too long: laurel_<slug> exceeds 100 chars`

### Requirement: GitHub create empty repository

`GitHubService.create_empty_repo(slug, private=False)` SHALL crear un
repo vacío en la organización `laurel-applications` con nombre
`laurel_<slug>`.

#### Scenario: Crear repo público exitosamente

- **WHEN** se invoca con `slug="mi-app"` y el nombre `laurel_mi-app`
  está disponible
- **THEN** retorna
  `{"name": "laurel_mi-app", "full_name": "laurel-applications/laurel_mi-app", "html_url": "https://github.com/laurel-applications/laurel_mi-app", "private": False}`
- **AND** el repo se crea sin commits ni archivos (salvo el `README.md`
  por defecto si la API lo requiere; se acepta ese único commit inicial)

#### Scenario: Nombre inválido

- **WHEN** el slug no cumple DNS-1123 (mayúsculas, espacios, caracteres
  especiales)
- **THEN** el servicio lanza `AppError` con `status_code=400` antes de
  llamar a GitHub

#### Scenario: Repo ya existe (422)

- **WHEN** GitHub responde 422 con `errors[0].code="name_already_exists"`
  para `laurel_<slug>`
- **THEN** el servicio lanza `AppError` con `status_code=409` y mensaje
  `GitHub repo 'laurel_<slug>' already exists`

### Requirement: GitHub repo_exists

`GitHubService.repo_exists(slug)` SHALL devolver `True`/`False` para el
repo `laurel-applications/laurel_<slug>` sin lanzar excepciones para 404.

#### Scenario: Repo existe

- **WHEN** el repo `laurel-applications/laurel_<slug>` existe
- **THEN** retorna `True`

#### Scenario: Repo no existe

- **WHEN** GitHub responde 404
- **THEN** retorna `False` (no es un error)

### Requirement: GitHub client uses requests directly

El cliente SHALL usar la librería `requests` (HTTP) directamente contra
la API REST de GitHub (`https://api.github.com`), sin SDKs adicionales.

#### Scenario: Endpoint base

- **WHEN** se construye el cliente
- **THEN** la URL base es `https://api.github.com` y el header
  `Authorization: token <pat>` se incluye en cada request

### Requirement: GitHub org is configurable

La organización donde se crean los repos SHALL ser configurable vía
`GITHUB_ORG` en el config (default `laurel-applications`). Cambiar el
valor no afecta repos ya creados.

#### Scenario: Override via config

- **WHEN** se setea `GITHUB_ORG=otro-org` en el config
- **THEN** todos los repos nuevos se crean bajo `otro-org`