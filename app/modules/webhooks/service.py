"""Logica del webhook de GitHub: verificar firma, bump de version, trigger Jenkins."""
import hashlib
import hmac


def _verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Valida `X-Hub-Signature-256` (HMAC-SHA256 hex del body)."""
    if not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _bump_version(current: str, sha: str = "") -> str:
    """Semver patch bump `x.y.z` -> `x.y.(z+1)`.

    Si `current` no es semver, usa el sha corto del push (`<sha[:7]>`)
    como nueva version; si no hay sha, deja `current` intacto.
    """
    parts = current.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return sha[:7] if sha else current
