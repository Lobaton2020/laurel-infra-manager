import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
API_PREFIX = "/api"

# Cargar .env antes de leer os.environ: el cuerpo de Config se evalua al importar.
# override=False para que las variables ya exportadas en el shell tengan prioridad.
load_dotenv(BASE_DIR / ".env", override=False)


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")
    API_PREFIX = API_PREFIX

    # --- Cluster K3s (ver K3S_CONTEXT.md) ---
    KUBECONFIG_PATH = os.environ.get("KUBECONFIG_PATH", str(BASE_DIR / "k3s.yaml"))
    # Al reiniciar K3s el kubeconfig vuelve a 127.0.0.1; se corrige en caliente con este valor.
    K8S_API_SERVER = os.environ.get("K8S_API_SERVER", "https://192.168.20.240:6443")
    K8S_VERIFY_SSL = os.environ.get("K8S_VERIFY_SSL", "true").lower() != "false"

    # --- Convenciones de despliegue (alineadas a deploy/base/ de Manejo-Finanzas) ---
    # Namespace unico donde conviven todos los scoops. Se auto-crea en el primer
    # deploy si no existe (ver DeployService.deploy).
    DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "user-apps")
    # El contenedor escucha aqui; el Service expone un 3xxx autoasignado y
    # hace targetPort al CONTAINER_PORT. Manejo-Finanzas usa 80 (PHP/Apache).
    CONTAINER_PORT = int(os.environ.get("CONTAINER_PORT", 80))
    SERVICE_TYPE = os.environ.get("SERVICE_TYPE", "LoadBalancer")
    SERVICE_PORT_RANGE_START = int(os.environ.get("SERVICE_PORT_RANGE_START", 3020))
    SERVICE_PORT_RANGE_END = int(os.environ.get("SERVICE_PORT_RANGE_END", 3999))
    HPA_TARGET_CPU = int(os.environ.get("HPA_TARGET_CPU", 80))

    # --- Subdominio publico (Traefik + cert-manager) ---
    # El DNS del cluster es un wildcard (*.andreslobaton.top → 192.168.20.240),
    # asi que crear el Ingress basta para publicar el scoop: ningun
    # registro DNS manual. cert-manager emite el certificado LetsEncrypt
    # (HTTP-01) apuntando a este Ingress.
    INGRESS_BASE_DOMAIN = os.environ.get("INGRESS_BASE_DOMAIN", "andreslobaton.top")
    INGRESS_CLASS = os.environ.get("INGRESS_CLASS", "traefik")
    CERT_MANAGER_CLUSTER_ISSUER = os.environ.get("CERT_MANAGER_CLUSTER_ISSUER", "letsencrypt-prod")

    # --- Override DNS interno (cert-manager HTTP-01 self-check) ---
    # K3s descubre zonas por /etc/hosts del nodo y genera bloques automáticos.
    # Para que un subdominio nuevo resuelva dentro del cluster a la IP LAN
    # (necesario para que cert-manager complete el HTTP-01 self-check), usamos
    # un ConfigMap importado por CoreDNS (`import /etc/coredns/custom/*.server`).
    # El API parchea ese ConfigMap en cada deploy para que el entry exista.
    DNS_OVERRIDE_CM_NAME = os.environ.get("DNS_OVERRIDE_CM_NAME", "coredns-custom")
    DNS_OVERRIDE_CM_NAMESPACE = os.environ.get("DNS_OVERRIDE_CM_NAMESPACE", "kube-system")
    DNS_OVERRIDE_FILE = os.environ.get("DNS_OVERRIDE_FILE", "andreslobaton.server")
    DNS_OVERRIDE_LAN_IP = os.environ.get("DNS_OVERRIDE_LAN_IP", "192.168.20.240")
    DNS_OVERRIDE_ZONE = os.environ.get("DNS_OVERRIDE_ZONE", "andreslobaton.top")

    # --- Base de datos ---
    DB_TYPE = os.environ.get("DB_TYPE", "sqlite")

    if DB_TYPE == "mysql":
        MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
        MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
        MYSQL_USER = os.environ.get("MYSQL_USER", "root")
        MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
        MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "laurel")

        SQLALCHEMY_DATABASE_URI = (
            f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}"
            f"@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DATABASE}"
        )
    else:
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{BASE_DIR}/laurel.db"

    SQLALCHEMY_TRACK_MODIFICATIONS = False
