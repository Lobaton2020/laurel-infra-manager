## Context

- Laurel-infra-manager es una API Flask monolítica con módulos por dominio
  (`scoops`, `configstore`, `system`, `cluster`); cada módulo sigue el
  patrón `model.py + schema.py + service.py + controller.py`.
- BD: SQLAlchemy con `db.Column(db.JSON/Integer/String)`; el proyecto
  actualmente NO usa Alembic, las migraciones se hacen vía `db.create_all()`
  y un script `migrate.py` con creación manual de columnas. Vamos a
  seguir el mismo patrón (sin introducir Alembic: YAGNI para un cambio
  de este tamaño).
- K3s: API Python con `kubernetes` oficial, los recursos se manejan
  vía `K8sService` (CRUD genérico) + `ClusterDNSService` (CoreDNS override).
- Cluster: `homelob` 192.168.20.240. Ya hay Cert-manager `letsencrypt-prod`
  y un wildcard DNS `*.andreslobaton.top`. CoreDNS se parchea en cada
  deploy para que el subdominio resuelva dentro del cluster.
- Frontend: Vite/React/TS; usa `api/` por dominio con `axios` ya
  configurado.

## Goals / Non-Goals

**Goals:**
- Entidad `Application` de primer nivel con CRUD + lifecycle de namespace
  dedicado.
- Force-delete con cascada robusta (no se cuelga, reporta progreso parcial).
- Backwards-compatible: scoops sin `application_id` siguen funcionando
  en `user-apps`.
- Integraciones GitHub + Docker Hub mínimas con PAT, sin OAuth ni push.
- Tests automatizados para cada flujo crítico (backfill, force-delete,
  PAT failure, slug collision).

**Non-Goals:**
- OAuth flow con GitHub.
- Push automático a Docker Hub / build pipeline.
- Multi-tenant / RBAC.
- Migración de secrets al cambiar de namespace (se recrean vía ConfigStore).
- GitOps / ArgoCD / Flux.
- Alembic (migración manual con script, como hace el resto del proyecto).

## Decisions

### D1. Application model con slug derivado del name

`Application.slug` se calcula con `slugify(name)` (función ya existente en
`scoops/schema.py`). El slug es la clave canónica para namespace y para
el nombre del repo GitHub. Si el usuario quiere un slug distinto al que
sale del name, hoy NO puede: trade-off aceptado para evitar un campo
más y mantener el namespace derivable determinísticamente.

**Alternativa considerada:** `slug` como campo editable por el usuario.
**Descartada:** añade fricción (validación de unicidad, edición
post-creación, riesgo de incoherencia namespace↔slug).

### D2. Scoop.application_id nullable, no enforced

La FK es **opcional** para no romper scoops legacy. La columna se agrega
con `nullable=True` y un índice (`ix_scoops_application_id`) para que los
listados filtrados por app sean rápidos. La migración hace backfill:
agrupa por `scoop.application` (string), calcula el slug, y crea una
`Application` por slug único. Si dos apps existentes sluggean igual,
la migración falla con error explícito y no se aplica.

**Alternativa considerada:** FK NOT NULL + script de migración manual.
**Descartada:** hace la migración destructiva para quien tenga scoops
sin app asociada.

### D3. Namespace resolution con prioridad explícita

`ManifestService.namespace_for(scoop, override=None)` resuelve con
prioridad: (1) `override` explícito del caller, (2) `scoop.namespace`
campo, (3) `application.slug` si `application_id` está set, (4)
`DEFAULT_NAMESPACE` ("user-apps"). Esto es un cambio mínimo: una sola
función, sin tocar más de 5 archivos que ya la llaman.

**Alternativa considerada:** que `scoop.namespace` siempre sea el campo
derivado (computed) y se persista. **Descartada:** complica el modelo,
hace la migración más invasiva, y no aporta nada: el cálculo es
barato.

### D4. Force-delete con cascada iterativa y timeout

`AppsService.force_delete(app_id)` sigue este orden:

1. **Undeploy de cada Domain de la app**: Ingress → Certificate → DNS
   override (responsabilidad del Domain).
2. Listar todos los recursos del namespace `app.slug` con label
   selector `app.kubernetes.io/managed-by=laurel-infra-manager`.
3. Para cada recurso: `K8sService.delete(kind, ns, name, missing_ok=True)`
   con timeout 5s por recurso.
4. Si quedan recursos después de 30s acumulados, retornar 207
   Multi-Status con `pending: [...]`.
5. `K8sService.delete_namespace(ns, timeout=10s)`.
6. Soft-delete la app en BD (`deleted_at=now()`).
7. `ScoopService.archive_for_app(app_id)` — pone `status="archived"`
   en todos los scoops con `application_id=app_id`.

Cada paso se mide y reporta. El orden es importante: primero los
Ingress/Certificate (porque dependen de Secrets y DNS), luego el resto
del namespace.

`AppsService.force_delete(app_id)` sigue este orden:

1. Listar todos los recursos del namespace `app.slug` con label
   selector `app.kubernetes.io/managed-by=laurel-infra-manager`.
2. Para cada recurso: `K8sService.delete(kind, ns, name, missing_ok=True)`
   con timeout 5s por recurso.
3. Si quedan recursos después de 30s acumulados, retornar 207
   Multi-Status con `pending: [...]`.
4. `K8sService.delete_namespace(ns, timeout=10s)`.
5. Soft-delete la app en BD (`deleted_at=now()`).
6. `ScoopService.archive_for_app(app_id)` — pone `status="archived"`
   en todos los scoops con `application_id=app_id`.

Cada paso se mide y reporta. El orden es importante: Deployment →
Service → Ingress → Certificate → ConfigMaps → Secrets → Namespace.
Ingress primero porque su Certificate depende de él.

**Alternativa considerada:** `kubectl delete namespace` y listo.
**Descartada:** no reporta progreso parcial y se cuelga con pods stuck.

### D5. PAT en system secrets, no en variables de entorno

Los PAT viven en Secrets K8s (whitelist `MANAGED`) igual que el resto
de credenciales (`laurel_env`, `laurel_kubeconfig`). El deployment
del sistema rollout-reinicia cuando se actualizan (mismo patrón que
`update_content()` actual).

**Alternativa considerada:** variables de entorno planas en `.env`.
**Descartada:** rompe la convención del proyecto, los PAT quedarían
versionados accidentalmente.

### D6. Cliente HTTP con `requests`, sin SDK

`GitHubService` y `DockerHubService` usan `requests` directo. Ya está
en `requirements.txt` (lo usa `app/modules/cluster/` para Cert-manager
en algunos paths). Sin `PyGithub` ni `docker-py`: cero deps nuevas,
cero superficie de mantenimiento.

### D7. Frontend: dropdown de apps en ScoopNew con fallback

`ScoopNew.tsx` carga `GET /api/apps` y muestra un `<select>` con las
apps existentes. Si la lista está vacía, muestra un input libre
(comportamiento legacy). Si el usuario escoge una app, los campos
`application` y `namespace` se autocompletan y bloquean.

### D8. Sin soft-delete global, pero con flag `deleted_at`

`Application.deleted_at` se setea al force-delete para auditoría y
para que el listado lo oculte. No se borra físicamente la fila.
El repo GitHub queda vivo en GitHub (no intentamos borrarlo desde
la API: eso es responsabilidad del usuario).

### D9. Domain es un recurso de primer nivel, no una propiedad del Scoop

La razón principal: no todos los scoops necesitan exposición pública
(workers, cronjobs no la necesitan), y la decisión de cuál scoop de
la app expone qué host se toma DESPUÉS de que los scoops existen.
Mezclar dominio con scoop o con app crea una dependencia que no se
puede deshacer: si el scoop X es el "principal" pero después el equipo
quiere exponer el scoop Y, hay que migrar.

**Alternativa considerada:** que el scoop `api` de la app genere
automáticamente `<slug>.<INGRESS_BASE_DOMAIN>`.
**Descartada:** es exactamente la dependencia que el usuario quiere
eliminar; además acopla deploy del scoop con emisión de cert (que
puede tardar minutos y fallar por DNS).

**Patrón resultante:** la API tiene su propio conjunto de endpoints
`/api/domains` y el flujo es:

1. Crear app.
2. Crear scoops (api/worker/cronjob) sin ingress.
4. Cuando se quiera exponer: crear Domain + `POST /api/domains/<id>/deploy`.

Esto desacopla 3 ciclos de vida: app (lifecycle largo), scoop (redeploys
frecuentes), domain (eventual; puede cambiar de scoop sin redeploy).

### D10. DomainService refactor de ManifestService.build()

`ManifestService.build()` deja de emitir `Ingress` y `Certificate`.
Solo emite los recursos del workload: `Deployment` (o `CronJob`),
`Service` (si `exposes_service`), `HorizontalPodAutoscaler` (si
`max_replicas > min_replicas`).

Las funciones `build_ingress`, `build_certificate`, `ingress_host`,
y `ClusterDNSService.add/remove` se mueven a `DomainService`.

`Scoop.host`/`Scoop.url` (computed fields) se eliminan del response
de la API; el cliente consulta `GET /api/domains?scoop_id=<id>` para
saber el estado de exposición.

**Trade-off:** refactor invasivo que rompe la API pública de scoops
(campos `host`/`url` desaparecen). Mitigation: el frontend está
controlado por nosotros; los tests existentes que dependen de
`build_certificate` se actualizan.

## Risks / Trade-offs

- **Slug collision en backfill**: dos apps con nombre que sluggean al
  mismo valor. Mitigation: validación pre-migración + error explícito.
- **Race condition en delete cascade**: si dos operadores hacen
  force-delete a la vez sobre la misma app. Mitigation: lock de fila
  en BD (`SELECT ... FOR UPDATE`) dentro de la transacción de delete.
- **PAT leak en logs**: si `requests` logea la URL con `?access_token=...`
  o si el log captura headers. Mitigation: el cliente construye URLs
  con header `Authorization` (no query string), y `AuditService.log`
  sanitiza `headers` antes de persistir.
- **GitHub rate limit (5000 req/h con PAT)**: suficiente para CRUD,
  pero no para sincronizaciones masivas. YAGNI: no sincronizamos.
- **Pods stuck en Terminating**: el cluster puede dejar pods
  atorados. Mitigation: timeout por recurso (5s) + timeout global
  (30s) + respuesta 207 con pendientes.
- **Costo de la FK adicional en listados**: si se filtra scoops por
  app frecuentemente, hay que indexar `application_id` (ya en D2).