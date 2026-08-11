"""Modulo Scoops.

Un scoop es la infraestructura de una aplicacion: la especificacion de lo que
debe correr (imagen, recursos, replicas, exposicion) y su materializacion como
manifiestos de Kubernetes.

  model.py      -> Scoop, la entidad persistida
  schema.py     -> DTOs de entrada/salida
  service.py    -> CRUD del catalogo y asignacion de puertos
  manifest.py   -> Scoop -> manifiestos de K8s
  deploy.py     -> aplicar/eliminar/reconciliar contra el cluster
  controller.py -> endpoints HTTP
"""
from app.modules.scoops.controller import bp
from app.modules.scoops.deploy import DeployService
from app.modules.scoops.manifest import ManifestService
from app.modules.scoops.model import Scoop
from app.modules.scoops.service import ScoopService

__all__ = ["bp", "Scoop", "ScoopService", "ManifestService", "DeployService"]