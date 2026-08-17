## Purpose

Whitelist de secretos administrados que se exponen vía UI/API. Crece con
dos secretos nuevos para autenticar las integraciones externas.

## MODIFIED Requirements

### Requirement: Managed secrets list (ADDED two entries)

La whitelist `MANAGED` SHALL incluir, además de los secretos actuales
(`laurel-secrets/.env`, `laurel-kubeconfig/k3s.yaml`), los siguientes:

| id | namespace | name | key | kind |
|---|---|---|---|---|
| `github_pat` | `laurel-infra-manager` | `laurel-github` | `pat` | text |
| `docker_pat` | `laurel-infra-manager` | `laurel-docker` | `pat` | text |

#### Scenario: Listar secretos del sistema

- **WHEN** `GET /api/system/secrets`
- **THEN** la respuesta incluye los 4 secretos: `laurel_env`,
  `laurel_kubeconfig`, `github_pat`, `docker_pat`

#### Scenario: Editar github_pat

- **WHEN** `PUT /api/system/secrets/github_pat` con `content="ghp_xxx..."`
- **THEN** el secret K8s `laurel-github` en `laurel-infra-manager` se
  parchea con `data.pat=<base64(content)>`
- **AND** el deployment del sistema se rollout-reinicia para que tome
  el nuevo valor

#### Scenario: PAT vacío

- **WHEN** `PUT /api/system/secrets/github_pat` con `content=""`
- **THEN** la API responde 400 con mensaje `content cannot be empty`