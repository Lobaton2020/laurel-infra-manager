## Purpose

ConfigMaps y Secrets de aplicación siguen siendo creados por app; ahora se
vinculan a la entidad `Application` por FK además del label, lo que permite
borrarlos en cascada cuando se elimina la app.

## MODIFIED Requirements

### Requirement: ConfigMap app validation (REPLACES `app` text validation)

El campo `app` en `ConfigMapCreate` y `SecretCreate` SHALL resolverse
contra `Application.slug`. Si no existe, la API responde 404.

#### Scenario: Crear ConfigMap con app existente

- **WHEN** `POST /api/configstore/configmaps` con `app="mi-app"` y la
  `Application` con `slug="mi-app"` existe
- **THEN** el ConfigMap se crea con label `app=mi-app` y FK
  `application_id=<id>`

#### Scenario: App no existe

- **WHEN** `POST /api/configstore/configmaps` con `app="no-existe"` y
  no hay `Application` con ese slug
- **THEN** la API responde 404 con mensaje `Application 'no-existe' not found`

### Requirement: ConfigMap/Secret cascade on app force-delete

Cuando se hace `DELETE /api/apps/<id>?force=true`, todos los
ConfigMaps y Secrets con `application_id=<id>` SHALL eliminarse del
namespace en el cluster antes de borrar el namespace.

#### Scenario: ConfigMap eliminado

- **WHEN** la app X se elimina con force y tiene 2 ConfigMaps vinculados
- **THEN** ambos ConfigMaps se eliminan del cluster antes del
  `delete namespace`
- **AND** los registros de BD se marcan como `deleted_at` (soft delete)