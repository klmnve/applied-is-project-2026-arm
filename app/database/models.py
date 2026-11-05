"""Related equipment tables and separate, validated API contracts."""

import unicodedata
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic import Field as ValidationField
from sqlalchemy import CheckConstraint
from sqlmodel import Field, Relationship, SQLModel

MAX_ID = 2**63 - 1
PositiveId = Annotated[int, ValidationField(strict=True, ge=1, le=MAX_ID)]
Quantity = Annotated[int, ValidationField(strict=True, ge=0, le=1_000_000_000)]
UnitCost = Annotated[
    float, ValidationField(strict=True, ge=0, le=1_000_000_000_000, allow_inf_nan=False)
]
Name = Annotated[str, ValidationField(min_length=1, max_length=160)]
InventoryCode = Annotated[str, ValidationField(min_length=1, max_length=64)]


class ResourceSeedState(SQLModel, table=True):
    """Internal marker prevents deleted demo equipment returning after restart."""

    name: str = Field(primary_key=True)


class ResourceCategory(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=160, unique=True, index=True)

    resources: list["DepartmentResource"] = Relationship(back_populates="category")


class ResourceRoom(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=160, unique=True, index=True)

    resources: list["DepartmentResource"] = Relationship(back_populates="room")


class DepartmentResource(SQLModel, table=True):
    __table_args__ = (
        CheckConstraint("length(trim(name)) BETWEEN 1 AND 160", name="resource_name_valid"),
        CheckConstraint(
            "length(trim(inventory_code)) BETWEEN 1 AND 64", name="resource_code_valid"
        ),
        CheckConstraint(
            "typeof(quantity) = 'integer' AND quantity BETWEEN 0 AND 1000000000",
            name="resource_quantity_valid",
        ),
        CheckConstraint("unit_cost BETWEEN 0 AND 1000000000000", name="resource_cost_valid"),
    )

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(max_length=160, index=True)
    inventory_code: str = Field(max_length=64, unique=True, index=True)
    category_id: int = Field(foreign_key="resourcecategory.id", index=True)
    room_id: int = Field(foreign_key="resourceroom.id", index=True)
    quantity: int = Field(ge=0)
    unit_cost: float = Field(ge=0)

    category: ResourceCategory = Relationship(back_populates="resources")
    room: ResourceRoom = Relationship(back_populates="resources")


class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "inventory_code", mode="before", check_fields=False)
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("Control characters are not allowed")
        return " ".join(value.split())


class ResourceCreate(ResourceInput):
    name: Name
    inventory_code: InventoryCode
    category_id: PositiveId
    room_id: PositiveId
    quantity: Quantity
    unit_cost: UnitCost


class ResourceUpdate(ResourceInput):
    name: Name | None = None
    inventory_code: InventoryCode | None = None
    category_id: PositiveId | None = None
    room_id: PositiveId | None = None
    quantity: Quantity | None = None
    unit_cost: UnitCost | None = None

    @model_validator(mode="after")
    def require_nonempty_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("At least one field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Resource fields cannot be null")
        return self


class ResourceRead(ResourceCreate):
    id: int
    category_name: str
    room_name: str


class ResourceItem(BaseModel):
    item: ResourceRead


class ResourcePage(BaseModel):
    items: list[ResourceRead]
    total: int
    page: int
    page_size: int
    pages: int


class ResourceReference(BaseModel):
    id: int
    name: str


class ResourceReferences(BaseModel):
    items: list[ResourceReference]
