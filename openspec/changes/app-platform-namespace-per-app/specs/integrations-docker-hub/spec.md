## Purpose

Cliente PAT-based para Docker Hub que valida la existencia de imágenes y
referencias. No realiza push automático ni builds: solo lectura y validación
para que el formulario de creación de apps pueda confirmar que la imagen
existe antes de registrar el scoop.

## ADDED Requirements

### Requirement: Docker Hub PAT authentication

El sistema SHALL autenticar todas las llamadas a Docker Hub usando un
Personal Access Token (PAT) almacenado en el secret del sistema
`docker_pat`. El token SHALL tener scope `read:repository` como mínimo.

#### Scenario: PAT no configurado

- **WHEN** `image_exists()` se invoca y el secret `docker_pat` no está
  configurado
- **THEN** el servicio retorna `None` (no puede verificar); el form de UI
  muestra un hint "PAT no configurado, no se puede verificar"

#### Scenario: PAT inválido

- **WHEN** Docker Hub responde 401
- **THEN** el servicio lanza `AppError` con `status_code=502`

### Requirement: Docker Hub image naming convention

Las imágenes de las apps SHALL seguir el patrón
`<DOCKER_HUB_NAMESPACE>/laurel_<slug>:<tag>` donde:

- `<DOCKER_HUB_NAMESPACE>` es configurable vía config
  (`DOCKER_HUB_NAMESPACE`, default `aflobaton`)
- `<slug>` es el slug DNS-1123 de la `Application`
- `<tag>` es la versión declarada en el scoop (default `latest`)

#### Scenario: Image ref generado

- **WHEN** `Application` con `name="Notas"` (slug `notas`) y un scoop con
  `version="v1.2.0"`
- **THEN** la imagen esperada es `aflobaton/laurel_notas:v1.2.0`

#### Scenario: Override del namespace

- **WHEN** se setea `DOCKER_HUB_NAMESPACE=mi-org` en el config
- **THEN** las imágenes se esperan bajo `mi-org/laurel_<slug>:<tag>`

### Requirement: Docker Hub validate image reference

`DockerHubService.validate_image_ref(image_ref)` SHALL validar que la
referencia tiene formato válido sin llamar a la API externa. El
`docker_image_base` declarado en la `Application` SHALL ser exactamente
`<namespace>/laurel_<slug>` (sin tag).

#### Scenario: Formato válido

- **WHEN** `image_ref="aflobaton/laurel_notas:latest"` o
  `image_ref="ghcr.io/owner/app:1.0"`
- **THEN** retorna `True`

#### Scenario: Formato inválido

- **WHEN** `image_ref="!!!@@@"` o vacío o sin tag cuando es requerido
- **THEN** retorna `False` y `validate_image_ref` retorna detalle del error

#### Scenario: docker_image_base sin prefijo laurel_

- **WHEN** `POST /api/apps` con `docker_image_base="aflobaton/notas"`
  (sin `laurel_`)
- **THEN** la API responde 400 con mensaje
  `docker_image_base must follow pattern '<namespace>/laurel_<slug>'`

### Requirement: Docker Hub image_exists

`DockerHubService.image_exists(image_ref)` SHALL consultar la registry
(API v2 de Docker Hub o registry v2 genérica para `ghcr.io`) y devolver
`True`/`False`.

#### Scenario: Imagen existe

- **WHEN** la imagen existe en la registry
- **THEN** retorna `True`

#### Scenario: Imagen no existe (404)

- **WHEN** la registry responde 404
- **THEN** retorna `False`

#### Scenario: Registry inaccesible

- **WHEN** la registry no responde en 5 segundos o retorna 5xx
- **THEN** el servicio retorna `None` (indeterminado) y registra warning
  en el log; el form de UI muestra "No se pudo verificar la imagen"

### Requirement: No push automático

El servicio SHALL NO incluir operaciones de push, build, tag o delete.
Solo lectura (`image_exists`, `validate_image_ref`).

#### Scenario: Intento de push

- **WHEN** un caller invoca `push_image()` (que no existe)
- **THEN** el sistema SHALL lanzar `AttributeError` claro (no se provee
  esa operación)