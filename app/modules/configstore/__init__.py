"""Modulo ConfigStore.

Gestiona ConfigMaps y Secrets de Kubernetes vinculados a una aplicacion
(el `application` de un Scoop). El nombre por convencion es `<app>-config`
para ConfigMap y `<app>-secret` para Secret; el caller puede sobreescribirlo.

Los recursos se materializan en el namespace del scoop y, al redeploy, se
inyectan automaticamente en su contenedor via `envFrom` (ver
`ManifestService._inject_app_env_from`).
"""
from app.modules.configstore.controller import bp
from app.modules.configstore.service import ConfigStoreService

__all__ = ["bp", "ConfigStoreService"]
