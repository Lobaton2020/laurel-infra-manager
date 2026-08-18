"""Logica del webhook de GitHub: verificar firma, bump de version, trigger Jenkins."""
import hashlib
import hmac
import json


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Valida `X-Hub-Signature-256` (HMAC-SHA256 hex del body).

    Workaround: Traefik 2.x re-bufea el body y lo re-emite con bytes
    ligeramente distintos (mismo contenido, distinto formato), rompiendo
    la firma HMAC. Para hacerlo robusto, intentamos validar primero sobre
    el body crudo. Si falla, re-serializamos el body como JSON canonico
    (separators compactos, sin espacios) y reintentamos.

    Esto es seguro: un atacante igual necesita conocer el secret para
    falsificar la firma. Re-serializar solo cambia la representacion,
    no la autenticidad.

    NOTA: la fix definitiva es configurar el Middleware de Traefik para
    deshabilitar el buffering. Ver deploy/overlays/prod/ingress-api.yml.
    """
    if not header:
        return False
    # Intento 1: firma sobre el body crudo
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, header):
        return True
    # Intento 2: firma sobre el body re-serializado (workaround Traefik)
    try:
        canonical = json.dumps(json.loads(body), separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    expected2 = "sha256=" + hmac.new(secret.encode(), canonical,
                                     hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected2, header)


def _bump_version(current: str, sha: str = "") -> str:
    """Semver patch bump `x.y.z` -> `x.y.(z+1)`.

    Si `current` no es semver, usa el sha corto del push (`<sha[:7]>`)
    como nueva version; si no hay sha, deja `current` intacto.
    """
    parts = current.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return sha[:7] if sha else current
