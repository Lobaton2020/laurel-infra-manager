# Tasks: deploy-strategies

> **Estado:** SPEC-ONLY — cambio de seguimiento futuro. Nada de esto se
> implementa en este change; se retoma en un sprint posterior. Todas las
> tareas quedan sin checkear a propósito. Para retomar: empezar por
> **Phase 1** y ejecutar de a una fase.

## Phase 1: Foundation — campo deploy_strategy + estrategias triviales

### 1.1. Modelo `Scoop.deploy_strategy`
- [ ] `app/modules/scoops/model.py`: columna
  `deploy_strategy = db.Column(db.String(20), nullable=False, default="rolling")`
  + constantes del enum (`rolling`, `blue_green`, `canary`, `recreate`)
- [ ] Migración: `ALTER TABLE scoops ADD COLUMN deploy_strategy
  VARCHAR(20) NOT NULL DEFAULT 'rolling'` (sin backfill)

### 1.2. Schemas + service
- [ ] `app/modules/scoops/schema.py`: `ScoopCreate`/`ScoopUpdate`/
  `ScoopResponse` agregan `deploy_strategy` (enum, default `"rolling"`);
  `DeployRequest` agrega `strategy` y `canary_replicas` opcionales
- [ ] `app/modules/scoops/service.py`: agregar `deploy_strategy` a los
  campos escalares del update y al tracked

### 1.3. Manifest: `spec.strategy`
- [ ] `app/modules/scoops/manifest.py`: `build_deployment` emite
  `spec.strategy` según `deploy_strategy`:
  - `recreate` → `{"type": "Recreate"}`
  - `rolling custom` → `{"type": "RollingUpdate",
    "rollingUpdate": {"maxSurge": X, "maxUnavailable": Y}}` con X/Y desde
    `DEPLOY_MAX_SURGE`/`DEPLOY_MAX_UNAVAILABLE`
  - `rolling` → comportamiento actual (default K8s)
- [ ] Tests `tests/test_manifests.py`: manifest de recreate y de rolling
  custom contienen el `spec.strategy` esperado

### 1.4. Controller
- [ ] `app/modules/scoops/controller.py`: `POST /api/scoops/<id>/deploy`
  acepta `{namespace, dry_run, strategy?, canary_replicas?}`; sin
  `strategy` usa `Scoop.deploy_strategy`
- [ ] Tests `tests/test_scoops.py`: deploy con `strategy="recreate"` pasa
  la estrategia al manifest

## Phase 2: Blue-green

### 2.1. Manifest con sufijo de generación
- [ ] `build_deployment(scoop, ns, name_suffix=None, gen=None)`:
  nombre `<name>-v<gen>` (gen truncado con `_sanitize_label`, ≤63 chars
  DNS-1123) y label discriminatoria inmutable
  `laurel.io/gen=<sanitized(version)>` en selector + template del pod
- [ ] `build_service`: Service conserva nombre `<name>`, mismo
  port/targetPort; selector incluye la label `gen` (parcheable)
- [ ] `build_hpa`: `scaleTargetRef` dinámico (apunta al gen activo) o HPA
  por gen
- [ ] Tests `tests/test_manifests.py`: nombres sufijados, label `gen` en
  selector, Service con nombre estable

### 2.2. DeployService: secuencia blue-green
- [ ] `DeployService.deploy`: cuando `deploy_strategy == "blue_green"`:
  (1) create/replace Deployment `vN+1` sin tráfico → (2) esperar
  `ready >= desired` (reusar lógica de status `deploy.py:179-193`) →
  (3) PATCH selector del Service a `gen` nuevo (switch atómico, sin
  downtime) → (4) borrar `vN` (ground) o conservarlo para rollback
- [ ] Tests `tests/test_deploy.py` (mock de `K8sService`): el PATCH del
  selector solo ocurre tras ready; el gen viejo se borra al final

### 2.3. Status con gen activo
- [ ] `status` (`deploy.py:141-207`): resolver el gen activo leyendo el
  selector del Service (fuente de verdad en el cluster) y reportar ambos
  Deployments en `pods`
- [ ] Tests `tests/test_deploy.py`: status blue-green reporta activo +
  reserva

### 2.4. Cleanup de generaciones
- [ ] `undeploy`: listar Deployments/HPA por label selector `app=<name>`
  y borrarlos todos (no solo `scoop.name`)
- [ ] `controller.py` delete del scoop (`controller.py:205-253`): el check
  de deploy activo lista por label `app=<name>` en vez de
  `exists("Deployment", ns, scoop.name)`
- [ ] Tests: undeploy/delete limpian generaciones residuales (vN y vN+1)

## Phase 3: Canary

### 3.1. Manifest canary
- [ ] `build_deployment` con sufijo `-canary`
- [ ] `build_service` omite la label `gen` del selector para canary (el
  Service matchea ambas generaciones; tráfico proporcional a pods Ready,
  gateado por readinessProbe)
- [ ] Tests `tests/test_manifests.py`: selector sin `gen` para canary

### 3.2. DeployService: canary + promoción
- [ ] `DeployService.deploy` canary: escala `<name>-canary` según
  `canary_replicas`; promoción = replace del Deployment primario a la
  versión nueva + scale canary a 0
- [ ] Tests `tests/test_deploy.py`: canary escala, promo reemplaza el
  primario y escala el canary a 0

## Phase 4: Verificación + integración

### 4.1. Tests E2E locales
- [ ] `pytest tests/test_manifests.py tests/test_deploy.py
  tests/test_scoops.py -v` → todo verde
- [ ] Curl E2E:
  - `POST /api/scoops/<id>/deploy` con `strategy="recreate"` → Deployment
    con `spec.strategy.type=Recreate`
  - Deploy blue-green → Deployment `-v<gen>`, Service con selector
    parcheado, ambos Deployments en status
  - Undeploy → sin Deployments residuales por label `app=<name>`

### 4.2. Documentación
- [ ] Actualizar `README.md` con la tabla de estrategias, defaults
  (`rolling`) y knobs (`DEPLOY_MAX_SURGE`/`DEPLOY_MAX_UNAVAILABLE`)

## Phase 5: Archive

### 5.1. OpenSpec archive
- [ ] `/opsx-archive deploy-strategies` (mergea delta specs a main specs)
  — SOLO cuando la implementación esté completa y verificada
