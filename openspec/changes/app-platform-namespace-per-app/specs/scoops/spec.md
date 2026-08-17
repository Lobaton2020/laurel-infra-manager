## Purpose

Los scoops siguen siendo el recurso operacional (api/worker/cronjob), pero
ahora se vinculan opcionalmente a una `Application` de primer nivel. Cuando
la FK está presente, el namespace se deriva de la app.

## MODIFIED Requirements

### Requirement: Scoop-application binding (REPLACES `Scoop.application` text)

El campo `Scoop.application` (string libre) sigue existiendo como etiqueta
humana legible, pero ahora coexiste con la FK `application_id` (int,
nullable, FK a `applications.id`). Las dos representaciones son
equivalentes para compatibilidad.

#### Scenario: Crear scoop con application_id

- **WHEN** `POST /api/scoops` con `application_id=42` y `application="X"`
- **THEN** el scoop se crea con `application_id=42`
- **AND** el campo `application` se valida para que sea consistente con
  la app vinculada (string del nombre o slug)

#### Scenario: Crear scoop sin application_id (legacy)

- **WHEN** `POST /api/scoops` con `application="legacy-app"` y sin
  `application_id`
- **THEN** el scoop se crea con `application_id=NULL`
- **AND** sigue funcionando como antes

### Requirement: Scoop namespace resolution (REPLACES `namespace_for` behavior)

`ManifestService.namespace_for(scoop, namespace=None)` SHALL resolver el
namespace así:

1. Si se pasa `namespace` explícito (query/body), se usa ese.
2. Si no, y `scoop.namespace` está set (override), se usa ese.
3. Si no, y `scoop.application_id` está set, se usa `application.slug`.
4. Si no, se usa `current_app.config["DEFAULT_NAMESPACE"]` (legacy).

#### Scenario: Scoop con application_id y sin override

- **WHEN** el scoop tiene `application_id=42` (app con `slug="mi-app"`)
  y `scoop.namespace=NULL`
- **THEN** `namespace_for()` retorna `"mi-app"`

#### Scenario: Scoop con override explícito

- **WHEN** el scoop tiene `scoop.namespace="custom-ns"`
- **THEN** `namespace_for()` retorna `"custom-ns"` (override gana)

#### Scenario: Scoop legacy sin app

- **WHEN** el scoop tiene `application_id=NULL` y `namespace=NULL`
- **THEN** `namespace_for()` retorna `"user-apps"` (legacy default)

### Requirement: Scoop status archived on app force-delete

Cuando se hace `DELETE /api/apps/<id>?force=true`, los scoops
`application_id=<id>` SHALL pasar a `status="archived"` y conservarse
en la BD para auditoría (no se borran físicamente).

#### Scenario: Scoop archivado

- **WHEN** la app X se elimina con force y el scoop Y estaba vinculado
- **THEN** `scoop.status` pasa a `"archived"`
- **AND** el scoop sigue apareciendo en `GET /api/scoops` con su filtro
  habitual (no se oculta)
- **AND** no se puede hacer `POST /api/scoops/<id>/deploy` sobre un
  scoop archivado (responde 400)

### Requirement: Scoop NO expone host/url/ingress/certificate

El modelo `Scoop` SHALL NO incluir campos `host`, `url`, ni ningún
estado de Ingress/Certificate. El deploy del scoop SHALL NO generar
Ingress ni Certificate: esos recursos son propiedad exclusiva del
recurso `Domain`.

#### Scenario: Response de Scoop no tiene host

- **WHEN** `GET /api/scoops/<id>` para un scoop con tipo `api`
- **THEN** el body NO incluye `host` ni `url`
- **AND** el cliente debe consultar `GET /api/domains?scoop_id=<id>`
  para saber si el scoop está expuesto públicamente

#### Scenario: Deploy de scoop no crea Ingress

- **WHEN** `POST /api/scoops/<id>/deploy` para un scoop de tipo `api`
- **THEN** el cluster recibe solo Deployment (+ Service si port +
  HPA si aplica)
- **AND** NO se crea Ingress, Certificate, ni DNS override
- **AND** el response NO incluye `host`, `dns_override`,
  `manual_hosts_lines`