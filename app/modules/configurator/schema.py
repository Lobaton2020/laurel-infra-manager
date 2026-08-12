"""Modelos Pydantic del modulo Configurator (validacion y serializacion).

Importado de `app/schemas.py` de configurator-lob.
"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

DataType = Literal["string", "number", "boolean", "json"]


class ColumnCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    data_type: DataType
    is_filterable: bool = True
    order: int = 0


class ColumnResponse(ColumnCreate):
    id: int
    schema_id: int
    created_at: datetime

    model_config = {"from_attributes": True}


class SchemaCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None


class SchemaResponse(SchemaCreate):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SchemaWithColumns(SchemaResponse):
    columns: list[ColumnResponse]


class RecordCreate(BaseModel):
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def validate_data(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("data cannot be empty")
        if "Value" not in v:
            raise ValueError("data must contain 'Value' key")
        return v


class RecordUpdate(BaseModel):
    data: dict[str, Any]


class RecordResponse(BaseModel):
    id: int
    schema_id: int
    data: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RecordListResponse(BaseModel):
    items: list[RecordResponse]
    total: int
    page: int
    limit: int
    pages: int
