import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from app.modules.scoops.model import STATUS_LABELS

ComponentType = Literal["api", "worker", "cronjob"]
ScoopStatus = Literal["active", "pending", "error"]
MemoryUnit = Literal["K", "M", "G", "T"]

# Un nombre de componente termina en labels y nombres de recursos: debe ser DNS-1123.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
# Cantidades de CPU de Kubernetes: "500m", "1", "1.5".
CPU_QUANTITY = re.compile(r"^\d+(\.\d+)?m?$")
# Cantidades de memoria: "128Mi", "1Gi", "512M". Aceptamos binario (Ki/Mi/Gi/Ti)
# y decimal (K/M/G/T); al guardar normalizamos a decimal.
MEMORY_QUANTITY = re.compile(r"^\d+(?:\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?$")

# Factores hacia megabytes decimales. K/M/G/T son decimales (base 1000);
# Ki/Mi/Gi/Ti son binarios (base 1024).
_TO_MB = {
    "K": 1 / 1000, "M": 1, "G": 1000, "T": 1000 * 1000,
    "Ki": 1 / 1024, "Mi": 1000 / 1024, "Gi": 1000 * 1000 / 1024, "Ti": 1000 ** 3 / 1024,
}


def _validate_cpu_quantity(value: str) -> str:
    if not CPU_QUANTITY.match(value):
        raise ValueError(f"'{value}' no es una cantidad de CPU valida (ej: '100m', '1', '1.5')")
    return value


def _validate_memory_quantity(value: str) -> str:
    if not MEMORY_QUANTITY.match(value):
        raise ValueError(f"'{value}' no es una cantidad de memoria valida (ej: '128M', '1Gi')")
    return value


def to_decimal_megabytes(value: str) -> tuple[int, MemoryUnit]:
    """Normaliza cualquier unidad de memoria a decimal (K/M/G/T).

    Devuelve (cantidad, unidad) elegidas para que el numero sea >= 1 y entero.
    Asi 128Mi -> 134M, 1Gi -> 1074M, 500M -> 500M.
    """
    m = MEMORY_QUANTITY.match(value)
    if not m:
        raise ValueError(f"'{value}' no es una cantidad de memoria valida")
    num = float(m.group(0).rstrip("KMGTikmgt"))
    unit = m.group(1) or "M"
    mb = num * _TO_MB[unit]

    if mb >= 1000 ** 3:
        return round(mb / 1000 ** 3), "T"
    if mb >= 1000 ** 2:
        return round(mb / 1000 ** 2), "G"
    if mb >= 1:
        return round(mb), "M"
    return round(mb * 1000), "K"


def format_memory(value: int, unit: MemoryUnit) -> str:
    """Compone un string K8s-compatible a partir de un valor entero y una unidad decimal."""
    return f"{value}{unit}"


def slugify(value: str) -> str:
    """Convierte texto libre en un nombre DNS-1123 valido."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:63].rstrip("-")
    return slug


class ScoopBase(BaseModel):
    # Opcional: si no llega, se deriva de `application`. Un scoop es la infra de
    # una aplicacion, asi que el nombre por defecto es el de la aplicacion.
    name: Optional[str] = Field(None, max_length=63)
    application: str = Field(..., min_length=1, max_length=100)
    type: ComponentType = "api"
    version: Optional[str] = Field(None, max_length=100)
    is_productive: bool = False

    requested_vcpu: str = "100m"
    # Memoria como (valor, unidad) para evitar que el frontend tenga que conocer
    # la sintaxis K8s. Almacenamos siempre el string equivalente en BD.
    requested_memory_value: int = Field(128, ge=1, le=999999)
    requested_memory_unit: MemoryUnit = "M"
    limit_vcpu: str = "500m"
    limit_memory_value: int = Field(512, ge=1, le=999999)
    limit_memory_unit: MemoryUnit = "M"

    min_replicas: int = Field(1, ge=0, le=100)
    max_replicas: int = Field(1, ge=1, le=100)

    url_registry: str = Field(..., min_length=1, max_length=255)
    namespace: Optional[str] = Field(None, max_length=63)
    schedule: Optional[str] = Field(None, max_length=100)

    # --- Agnóstico de tecnología ---
    # Puerto que expone la imagen. Si está, se genera Service con targetPort.
    # Si no, el pod corre solo y se accede internamente por su nombre DNS.
    container_port: Optional[int] = Field(None, ge=1, le=65535)
    # Path HTTP para readiness/liveness. Requiere container_port.
    health_path: Optional[str] = Field(None, max_length=255)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not DNS_LABEL.match(v):
            raise ValueError(
                "name debe ser minusculas, numeros y guiones, empezando y "
                "terminando en alfanumerico (formato DNS-1123)"
            )
        return v

    @field_validator("requested_vcpu", "limit_vcpu")
    @classmethod
    def _v_cpu(cls, v: str) -> str:
        return _validate_cpu_quantity(v)

    @model_validator(mode="after")
    def validate_consistency(self):
        if not self.name:
            derived = slugify(self.application)
            if not DNS_LABEL.match(derived or ""):
                raise ValueError(
                    f"no se pudo derivar un nombre valido de application='{self.application}'; "
                    "envia 'name' explicitamente"
                )
            self.name = derived
        if self.max_replicas < self.min_replicas:
            raise ValueError("max_replicas no puede ser menor que min_replicas")
        if self.type == "cronjob" and not self.schedule:
            raise ValueError("schedule es obligatorio para componentes de tipo 'cronjob'")
        return self


class ScoopCreate(ScoopBase):
    """El puerto no se acepta del cliente: lo asigna el servidor desde el rango 3xxx."""


class ScoopUpdate(BaseModel):
    """Actualizacion parcial. `name` y `type` son inmutables: cambiarlos dejaria
    recursos huerfanos en el cluster."""

    application: Optional[str] = Field(None, min_length=1, max_length=100)
    version: Optional[str] = Field(None, max_length=100)
    status: Optional[ScoopStatus] = None
    is_productive: Optional[bool] = None

    requested_vcpu: Optional[str] = None
    requested_memory_value: Optional[int] = Field(None, ge=1, le=999999)
    requested_memory_unit: Optional[MemoryUnit] = None
    limit_vcpu: Optional[str] = None
    limit_memory_value: Optional[int] = Field(None, ge=1, le=999999)
    limit_memory_unit: Optional[MemoryUnit] = None

    min_replicas: Optional[int] = Field(None, ge=0, le=100)
    max_replicas: Optional[int] = Field(None, ge=1, le=100)

    url_registry: Optional[str] = Field(None, min_length=1, max_length=255)
    namespace: Optional[str] = Field(None, max_length=63)
    schedule: Optional[str] = Field(None, max_length=100)

    container_port: Optional[int] = Field(None, ge=1, le=65535)
    health_path: Optional[str] = Field(None, max_length=255)

    @field_validator("requested_vcpu", "limit_vcpu")
    @classmethod
    def _v_cpu(cls, v: Optional[str]) -> Optional[str]:
        return _validate_cpu_quantity(v) if v is not None else v

    @model_validator(mode="after")
    def validate_replicas(self):
        if (
            self.min_replicas is not None
            and self.max_replicas is not None
            and self.max_replicas < self.min_replicas
        ):
            raise ValueError("max_replicas no puede ser menor que min_replicas")
        return self


class ScoopResponse(BaseModel):
    id: int
    name: str
    application: str
    type: ComponentType
    status: ScoopStatus
    version: Optional[str] = None
    is_productive: bool

    requested_vcpu: str
    requested_memory: str
    limit_vcpu: str
    limit_memory: str

    # Memoria desglosada: la UI muestra numero y unidad por separado sin parsear.
    requested_memory_value: int
    requested_memory_unit: MemoryUnit
    limit_memory_value: int
    limit_memory_unit: MemoryUnit

    min_replicas: int
    max_replicas: int

    url_registry: str
    port: Optional[int] = None
    namespace: str
    schedule: Optional[str] = None

    container_port: Optional[int] = None
    health_path: Optional[str] = None

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_scoop(cls, scoop) -> "ScoopResponse":
        req_val, req_unit = to_decimal_megabytes(scoop.requested_memory)
        lim_val, lim_unit = to_decimal_megabytes(scoop.limit_memory)
        return cls(
            id=scoop.id,
            name=scoop.name,
            application=scoop.application,
            type=scoop.type,
            status=scoop.status,
            version=scoop.version,
            is_productive=scoop.is_productive,
            requested_vcpu=scoop.requested_vcpu,
            requested_memory=scoop.requested_memory,
            limit_vcpu=scoop.limit_vcpu,
            limit_memory=scoop.limit_memory,
            requested_memory_value=req_val,
            requested_memory_unit=req_unit,
            limit_memory_value=lim_val,
            limit_memory_unit=lim_unit,
            min_replicas=scoop.min_replicas,
            max_replicas=scoop.max_replicas,
            url_registry=scoop.url_registry,
            port=scoop.port,
            namespace=scoop.namespace,
            schedule=scoop.schedule,
            container_port=scoop.container_port,
            health_path=scoop.health_path,
            created_at=scoop.created_at,
            updated_at=scoop.updated_at,
        )

    @computed_field
    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


class ScoopListResponse(BaseModel):
    items: list[ScoopResponse]
    total: int
    page: int
    limit: int
    pages: int


class ScoopStatusResponse(BaseModel):
    """Estado del catalogo (BD) contrastado con el estado real del cluster."""

    scoop: ScoopResponse
    deployed: bool
    namespace: str
    desired_replicas: Optional[int] = None
    ready_replicas: Optional[int] = None
    available_replicas: Optional[int] = None
    pods: list[dict[str, Any]] = Field(default_factory=list)
    message: Optional[str] = None


class DeployRequest(BaseModel):
    namespace: Optional[str] = Field(None, max_length=63)
    dry_run: bool = False
