"""Modulo System: endpoints para gestionar recursos del propio backend.

Concretamente: edicion de los secretos del sistema que monta el deployment
(laurel-secrets, laurel-kubeconfig). Esta whitelistado por codigo, no por
config del cluster, asi el endpoint nunca puede tocar secretos ajenos.
"""
from app.modules.system.controller import bp

__all__ = ["bp"]
