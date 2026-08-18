"""Modulo webhooks: callbacks entrantes de servicios externos (GitHub).

`bp`     -> webhook real (/api/webhooks/github), HMAC validado.
`dev_bp` -> endpoint de dev para simular pushes localmente
            (/api/dev/simulate-push), JWT-gated. Pensado para probar
            el flujo end-to-end desde el front sin un push real.
"""
from app.modules.webhooks.controller import bp, dev_bp

__all__ = ["bp", "dev_bp"]
