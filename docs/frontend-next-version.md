# Frontend: versionado de imágenes (auto-increment)

Contrato entre el backend `laurel-infra-manager` y el frontend para que el
**usuario nunca edite una versión**. El backend calcula la próxima versión
desde Docker Hub; el frontend la muestra como dato de solo lectura y
dispara builds que usan esa versión automáticamente.

## Regla de oro

> **Nunca** renderizar un `<input>` ni un selector para que el operador
> tipee/edite el número de versión. La versión es 100% responsabilidad del
> backend. Si el front necesita una versión, la pide al endpoint y la
> muestra tal cual.

## Endpoint backend

```
GET /api/apps/<slug>/next_version
Authorization: Bearer <jwt>
```

### Respuesta 200

```json
{
  "slug": "notas-test",
  "namespace": "aflobaton",
  "image": "aflobaton/laurel_notas-test",
  "next_version": "0.0.3"
}
```

### Errores

| Status | `reason` | Significado | UX |
|---|---|---|---|
| 400 | `invalid_slug` | Slug con formato inválido | Mostrar "slug inválido" en línea |
| 503 | `dockerhub_unconfigured` | Backend sin `DOCKERHUB_USER/PASSWORD` | Banner admin-only |
| 502 | `dockerhub_error` | Docker Hub rechazó login / tags | Toast de error + retry |

Validación del slug: `^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$`
(máx 63 chars, sin `_`/`-` al inicio/fin, sin mayúsculas ni unicode).

### Caché recomendada

La versión auto-incrementa cuando se pushea una build, así que cachear el
`next_version` en el front más de 30 s provoca que el operador vea la
versión anterior justo después de un push. Sugerencia:

- **TTL ≤ 30 s** o **invalidate on build trigger** (limpiar la cache al
  disparar un build y volver a pedir el endpoint).
- No cachear entre usuarios: el endpoint ya está scoped por slug.

## UX en la pantalla de detalle de la app

```
┌─────────────────────────────────────────────────────────────┐
│  App: notas-test                                            │
│  ─────────────────────────────────────────────────────────  │
│                                                             │
│  Próxima versión (auto)                                     │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  v0.0.3                                              │    │  ← read-only badge
│  │  imagen destino: aflobaton/laurel_notas-test:v0.0.3  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  [  ▶ Trigger build  ]   ← click → backend usa v0.0.3      │
│                                                             │
│  Último build                                               │
│   • #2  dd0997e3  (v0.0.2)  ✓ success                       │
│   • #1  dd0997e3  (v0.0.1)  ✓ success                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Lo que el front **debe** hacer

1. **Badge de solo lectura** con `next_version` (chip / pill).
2. Mostrar el `image` destino calculado (`<namespace>/laurel_<slug>`).
3. Botón **"Trigger build"** que llama al endpoint de trigger del
   backend (NO al endpoint de next_version; next_version es solo info).
4. Al volver a la pantalla (focus, polling cada 30 s, o al volver del
   trigger), refetchear el endpoint para reflejar el nuevo `next_version`
   que el backend ya calculó a partir del push anterior.

### Lo que el front **no debe** hacer

- ❌ Input numérico de versión.
- ❌ Selector de versión (`<select>` con `0.0.1 / 0.0.2 / ...`).
- ❌ Botón "Save version" / "Override version".
- ❌ Cachear el endpoint por más de 30 s sin invalidar.
- ❌ Concatenar strings para armar la versión (`"0.0." + String(n + 1)`).
- ❌ Mostrar la versión antes de tiempo (antes de un trigger real).

## Snippet de referencia (React + fetch)

```tsx
import { useEffect, useState } from "react";

type NextVersion = {
  slug: string;
  namespace: string;
  image: string;
  next_version: string;
};

type ApiError = { error: string; reason?: string; status: number };

async function fetchNextVersion(slug: string, token: string): Promise<NextVersion> {
  const r = await fetch(`/api/apps/${slug}/next_version`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store", // desactiva cache HTTP del browser
  });
  const body = await r.json();
  if (!r.ok) {
    const err: ApiError = { ...body, status: r.status };
    throw err; // reason: 'invalid_slug' | 'dockerhub_unconfigured' | 'dockerhub_error'
  }
  return body as NextVersion;
}

export function NextVersionBadge({ slug, token, onTrigger }: {
  slug: string;
  token: string;
  onTrigger: () => Promise<void>;
}) {
  const [nv, setNv] = useState<NextVersion | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);

  useEffect(() => {
    let cancel = false;
    async function poll() {
      try {
        const data = await fetchNextVersion(slug, token);
        if (!cancel) setNv(data);
      } catch (e: any) {
        if (!cancel) setErr(e);
      }
    }
    poll();
    const t = setInterval(poll, 30_000); // refresh cada 30s
    return () => { cancel = true; clearInterval(t); };
  }, [slug, token]);

  if (err) {
    return (
      <div role="alert" className="text-error">
        No se pudo calcular la próxima versión
        ({err.reason ?? err.error}). Reintentando…
      </div>
    );
  }
  if (!nv) return <span>cargando…</span>;

  return (
    <div>
      <span className="badge" aria-readonly="true">
        v{nv.next_version}
      </span>
      <code className="text-muted">{nv.image}:v{nv.next_version}</code>
      <button onClick={onTrigger}>▶ Trigger build</button>
    </div>
  );
}
```

Puntos clave del snippet:

- `cache: "no-store"` evita que el browser cachee entre polls.
- `setInterval(30_000)` mantiene la versión fresca sin esperar interacción.
- El badge **no tiene handler de edición**; es semánticamente de solo
  lectura (`aria-readonly="true"` como pista para a11y).
- `onTrigger` se invoca al click pero el front **no manipula** la versión
  antes de mandar; el backend la recalcula al ejecutar el build.

## Por qué este diseño

- **Una sola fuente de verdad** (Docker Hub): si varios operadores disparan
  builds concurrentemente, la versión auto-incrementada garantiza que no
  colisionan tags en `docker.io/.../<repo>`.
- **Independiente del PAT**: la versión funciona aunque el GitHub PAT
  sea read-only (no depende de `git tag`).
- **Resiliente a reintentos**: si una build falla, la siguiente
  `next_version` se calcula correctamente sin "saltar" un número.
- **Auditabilidad**: cada push deja un tag semver en Docker Hub que
  sirve de historial inmutable.

## Referencias

- Backend: `app/modules/apps/controller.py::next_version_for_slug`
- Módulo core: `app/modules/integrations/docker/version_bump.py`
- POC end-to-end: `/tmp/opencode/poc_local_driver.py` + `/tmp/opencode/poc_orchestrator.py`
- Sugerencias servidor Jenkins: `/tmp/opencode/JENKINS_SERVER_SUGGESTIONS.md`