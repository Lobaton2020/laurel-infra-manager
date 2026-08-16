"""DTOs del modulo ConfigStore (ConfigMaps y Secrets vinculados a una app)."""
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Mismo patron que el resto del proyecto: DNS-1123 para nombres de recursos.
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def _validate_name(v: str) -> str:
    if not DNS_LABEL.match(v):
        raise ValueError(
            "name debe ser minusculas, numeros y guiones, empezando y "
            "terminando en alfanumerico (formato DNS-1123)"
        )
    return v


class ConfigMapCreate(BaseModel):
    """Crea o reemplaza un ConfigMap de aplicacion.

    `app` es el `application` del Scoop al que se vincula. Por convencion, el
    recurso se nombra `<app>-config` salvo que el caller indique `name`.
    """
    app: str = Field(..., min_length=1, max_length=100)
    name: str | None = Field(None, max_length=63)
    namespace: str | None = Field(None, max_length=63)
    data: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        return _validate_name(v) if v is not None else v

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not key:
                raise ValueError("las claves de data no pueden estar vacias")
        return v


class ConfigMapUpdate(BaseModel):
    """Reemplazo total del `data` del ConfigMap (PUT semantico)."""
    data: dict[str, str] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not key:
                raise ValueError("las claves de data no pueden estar vacias")
        return v


class SecretCreate(BaseModel):
    """Crea o reemplaza un Secret de aplicacion.

    `data` debe llegar en base64 (asi lo almacena K8s en su API). El caller
    es responsable de base64-encodear los valores: esto evita que la API
    tenga que distinguir binario vs texto por nosotros, y mantiene la
    compatibilidad con cualquier cliente que ya hable el formato K8s.
    """
    app: str = Field(..., min_length=1, max_length=100)
    name: str | None = Field(None, max_length=63)
    namespace: str | None = Field(None, max_length=63)
    data: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _v_name(cls, v: str | None) -> str | None:
        return _validate_name(v) if v is not None else v

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not key:
                raise ValueError("las claves de data no pueden estar vacias")
        return v


class SecretUpdate(BaseModel):
    data: dict[str, str] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: dict[str, str]) -> dict[str, str]:
        for key in v:
            if not key:
                raise ValueError("las claves de data no pueden estar vacias")
        return v


class ConfigMapResponse(BaseModel):
    name: str
    namespace: str
    app: str
    data: dict[str, str]
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None

    model_config = {"from_attributes": True}


class SecretResponse(BaseModel):
    """Respuesta de un Secret: nunca incluye `data`.

    Solo se exponen metadatos y la lista de claves existentes: el contenido
    sensible nunca sale de la API. Para editar, el cliente usa PUT con el
    `data` completo.
    """
    name: str
    namespace: str
    app: str
    keys: list[str]
    labels: dict[str, str] = Field(default_factory=dict)
    created_at: str | None = None

    model_config = {"from_attributes": True}


class ConfigMapSummary(BaseModel):
    """Resumen de un ConfigMap para listados (omite data para no inflar la respuesta)."""
    name: str
    namespace: str
    app: str
    keys: list[str]
    created_at: str | None = None


class SecretSummary(BaseModel):
    name: str
    namespace: str
    app: str
    keys: list[str]
    created_at: str | None = None
