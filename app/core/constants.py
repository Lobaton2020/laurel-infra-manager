"""Constantes compartidas entre modulos.

Viven en core para que `cluster` (capa baja) no tenga que importar de `scoops`
(capa alta) y evitar dependencias circulares.
"""

# Marca los recursos creados por este API, para distinguirlos de los que ya
# existian en el cluster.
MANAGED_BY = "laurel-infra-manager"
MANAGED_BY_SELECTOR = f"app.kubernetes.io/managed-by={MANAGED_BY}"

# Prefijo de los labels propios del proyecto.
LABEL_PREFIX = "laurel.andrelobaton.top"
