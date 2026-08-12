"""Registro DNS interno para los subdominios de los scoops.

Mantiene el ConfigMap que CoreDNS importa (`/etc/coredns/custom/*.server`).
El cluster resuelve la zona via el plugin `hosts` del bloque, lo que garantiza
que cert-manager complete el HTTP-01 self-check desde un pod del cluster.

Topologia esperada (ver K3S_CONTEXT.md):
- K3s detecta `andreslobaton.top` en /etc/hosts del nodo y genera un server
  block automatico con `forward .`. Eso apunta al wildcard publico
  (181.52.14.239) que no responde desde la red LAN del cluster (hairpin NAT).
- Este ConfigMap sobreescribe ese bloque con un plugin `hosts` que mapea
  subdominios concretos a la IP LAN, y un `fallthrough` para los hosts
  no listados.
"""
import logging
import re

from kubernetes.client.exceptions import ApiException

from app.core.k8s import get_clients

logger = logging.getLogger(__name__)


class ClusterDNSError(Exception):
    """No se pudo leer/escribir el ConfigMap DNS override."""


class ClusterDNSService:
    """Maneja el ConfigMap `coredns-custom` con la zona andreslobaton.top."""

    @staticmethod
    def _read_cm() -> tuple[str, str]:
        """Lee el ConfigMap y devuelve (configmap_name, contenido_data[file]).

        La ruta del CM se resuelve de la config del app para poder testearlo
        contra un ConfigMap paralelo si hace falta.
        """
        from flask import current_app

        clients = get_clients()
        name = current_app.config["DNS_OVERRIDE_CM_NAME"]
        namespace = current_app.config["DNS_OVERRIDE_CM_NAMESPACE"]
        try:
            cm = clients.core.read_namespaced_config_map(name, namespace)
        except ApiException as exc:
            if exc.status == 404:
                raise ClusterDNSError(
                    f"No existe el ConfigMap '{namespace}/{name}'. "
                    "Aplicale primero el `kube-system/coredns-custom` con el "
                    "server block `andreslobaton.server` (ver K3S_CONTEXT.md)."
                ) from exc
            raise
        file_key = current_app.config["DNS_OVERRIDE_FILE"]
        body = cm.data.get(file_key) if cm.data else None
        if not body:
            raise ClusterDNSError(
                f"El ConfigMap '{namespace}/{name}' no tiene la clave '{file_key}'"
            )
        return name, body

    @staticmethod
    def _write_cm(body: str) -> None:
        from flask import current_app

        clients = get_clients()
        name = current_app.config["DNS_OVERRIDE_CM_NAME"]
        namespace = current_app.config["DNS_OVERRIDE_CM_NAMESPACE"]
        file_key = current_app.config["DNS_OVERRIDE_FILE"]

        cm = clients.core.read_namespaced_config_map(name, namespace)
        if cm.data is None:
            cm.data = {}
        cm.data[file_key] = body
        clients.core.replace_namespaced_config_map(name, namespace, cm)
        logger.info("ConfigMap '%s/%s' actualizado", namespace, name)

    # ---------- parseo / render ----------

    _HOSTS_RE = re.compile(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([A-Za-z0-9._-]+)\s*$")

    @classmethod
    def _parse_entries(cls, body: str) -> dict[str, str]:
        """Lee el contenido del server block y devuelve {host: ip} de los hosts.

        Tolera lineas en blanco y comentarios. Si un host aparece dos veces gana
        la ultima ocurrencia. Si el bloque `hosts {...}` no existe devuelve dict
        vacio (se renderiza uno nuevo al guardar).
        """
        if not body:
            return {}
        in_hosts = False
        entries: dict[str, str] = {}
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("hosts") and stripped.endswith("{"):
                in_hosts = True
                continue
            if in_hosts:
                if stripped == "fallthrough" or stripped.startswith("fallthrough"):
                    continue
                if stripped == "}":
                    in_hosts = False
                    continue
                m = cls._HOSTS_RE.match(line)
                if m:
                    ip, host = m.group(1), m.group(2)
                    entries[host] = ip
        return entries

    @classmethod
    def _render(cls, zone: str, entries: dict[str, str]) -> str:
        """Renderiza el server block completo de la zona."""
        lines = [f"{zone} {{", "  hosts {"]
        for host in sorted(entries):
            lines.append(f"    {entries[host]} {host}")
        if not entries:
            lines.append("    # (vacio)")
        lines.append("    fallthrough")
        lines.append("  }")
        lines.append("  forward . /etc/resolv.conf")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ---------- API publica ----------

    @classmethod
    def list_hosts(cls) -> dict[str, str]:
        """Devuelve un dict {host: ip} con todos los subdominios registrados."""
        _, body = cls._read_cm()
        return cls._parse_entries(body)

    @classmethod
    def add(cls, host: str, ip: str) -> str:
        """Agrega o actualiza la entrada `<ip> <host>` en el server block.

        Idempotente: si ya existe con la misma ip no toca el archivo. Si
        existe con otra ip la actualiza. Devuelve 'added' | 'unchanged' | 'updated'.
        """
        _, body = cls._read_cm()
        entries = cls._parse_entries(body)

        from flask import current_app

        zone = current_app.config["DNS_OVERRIDE_ZONE"]

        if entries.get(host) == ip:
            return "unchanged"
        was_present = host in entries
        entries[host] = ip
        cls._write_cm(cls._render(zone, entries))
        return "updated" if was_present else "added"

    @classmethod
    def remove(cls, host: str) -> str:
        """Quita la entrada del server block. Idempotente.

        Devuelve 'removed' si la quito o 'absent' si ya no estaba.
        """
        from flask import current_app

        _, body = cls._read_cm()
        entries = cls._parse_entries(body)
        if host not in entries:
            return "absent"
        del entries[host]
        cls._write_cm(cls._render(current_app.config["DNS_OVERRIDE_ZONE"], entries))
        return "removed"
