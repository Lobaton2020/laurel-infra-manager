"""Modulo Cluster: acceso directo a recursos nativos de Kubernetes.

No tiene modelo propio (el cluster es la fuente de verdad); expone el API de K8s
al frontend y sirve de capa de acceso para el modulo scoops.
"""
from app.modules.cluster.controller import bp
from app.modules.cluster.service import K8sService, kind_ops

__all__ = ["bp", "K8sService", "kind_ops"]
