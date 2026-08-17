# laurel-infra-manager

API de infraestructura sobre Kubernetes (K3s `homelob`). Gestiona un catalogo
de scoops (CSS frente a apps desplegables) y los materializa como
Deployments, Services, HPAs e Ingresses, ademas de un modulo Configurator
(schemas/records) y auditoria de todos los cambios.

- **API**: Flask + SQLAlchemy + Pydantic, documentacion con Flasgger en `/apidocs`.
- **BBDD**: MySQL/MariaDB (prod) o sqlite (dev).
- **Auth**: Google Sign-In (id_token verificado) + JWT propio (stateless).
- **Frontend**: vive en un repo aparte (`configurator-lob/frontend`).

## Estructura

```
app/
├── core/          # db, http helpers, auth, errores, health
└── modules/
    ├── auth/      # Google OAuth + JWT
    ├── users/     # usuarios conocidos
    ├── scoops/    # catalogo + life-cycle (manifests, deploy)
    ├── cluster/   # informacion del cluster (nodes, pods, services...)
    ├── configurator/  # schemas/columns/records + stats
    └── audits/    # traza de mutaciones (guarda email del autor)
deploy/            # manifests Kustomize (solo prod)
.github/workflows/ # CI: build y push de la imagen a Docker Hub
```

## Configuracion

Copia `.env.example` a `.env` y ajusta. Variables clave:

| Variable | Descripcion |
|---|---|
| `DB_TYPE` | `sqlite` (dev) o `mysql` (prod) |
| `MYSQL_*` | conexion a la base si `DB_TYPE=mysql` |
| `KUBECONFIG_PATH` | kubeconfig del cluster (default `./k3s.yaml`, no se versiona) |
| `GOOGLE_CLIENT_ID` | OAuth Client ID de Google (debe coincidir con el del front) |
| `SECRET_KEY` | firma del JWT (>= 32 chars en prod) |
| `AUTH_ALLOWED_ORIGINS` | origins del front permitidos por CORS |

## Correr local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# Dev (auto-reload en 5002)
python run.py

# Prod-like con gunicorn
gunicorn -b 0.0.0.0:5002 -w 2 --timeout 60 run:app
```

Tests:

```bash
pytest
```

## Imagen Docker y CI

`Dockerfile` multi-stage (python 3.12-slim, usuario non-root `10001`,
gunicorn en `:5002`). `.dockerignore` evita filtrar `.env`, `k3s.yaml` o `*.db`
al contexto de build.

El workflow `.github/workflows/docker-image.yml` construye y publica en Docker Hub:

- Push a la rama por defecto -> `aflobaton/laurel-infra-manager:latest` + `sha-<sha>`.
- PR -> solo build de validacion (sin push).

Requiere los secrets `DOCKERHUB_USERNAME` y `DOCKERHUB_TOKEN`
(repo Settings > Secrets and variables > Actions). El primer push a una rama
debe completarse para que exista la imagen `latest`.

## Deploy en el cluster (solo prod)

La app se publica por **LoadBalancer en el puerto 3006** (mismo patron que
Manejo-Finanzas: IP del nodo `192.168.20.240`, sin Ingress). Acceso:
`http://192.168.20.240:3006/api`.

### 1. Crear los Secrets (una vez)

```bash
# env con las mismas variables de config.py (DB, auth, cluster)
kubectl create secret generic laurel-secrets \
  --from-file=.env=./.env \
  -n prod

# kubeconfig del cluster para que la API hable con K8s
kubectl create secret generic laurel-kubeconfig \
  --from-file=k3s.yaml=./k3s.yaml \
  -n prod
```

Detalle en `deploy/base/secret.example.yml`. Ni `.env` ni `k3s.yaml` se
versionan (`.gitignore`).

### 2. Aplicar

```bash
kubectl apply -k deploy/overlays/prod
```

### 3. Verificar

```bash
kubectl get deploy,svc -n prod
kubectl rollout status deploy/laurel-infra-manager -n prod
curl http://192.168.20.240:3006/api/health
```

## DNS override (coredns-custom)

Subdominios publicos que deben resolver dentro del cluster (HTTP-01 de
cert-manager). La API parchea este ConfigMap al desplegar; para agregar uno
manualmente en el nodo, sin pisar las entradas existentes:

```bash
sudo kubectl -n kube-system get configmap coredns-custom -o yaml \
  | sed '0,/192.168.20.240 tmp.andreslobaton.top/s//&\n        192.168.20.240 <dominio>.andreslobaton.top/' \
  | sudo kubectl apply -f - \
  && sudo kubectl -n kube-system rollout restart deployment coredns
```

Equivalente por la app (idempotente, no necesita acceso al nodo):

```bash
.venv/bin/python -c "
from app import create_app
from app.modules.dns.service import ClusterDNSService
app = create_app()
app.app_context().push()
print(ClusterDNSService.add('<dominio>.andreslobaton.top', '192.168.20.240'))
"
```