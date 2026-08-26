"""Typed Phase 2 models for the narrow peristaltic-dispense slice."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FlowRate(str, Enum):
    LOW = "low"
    MEDIUM = "medium"


class CassetteType(str, Enum):
    ANY = "any"
    ONE_UL = "1ul"
    FIVE_UL = "5ul"
    TEN_UL = "10ul"


CASSETTE_CODES = {
    CassetteType.ANY: 0,
    CassetteType.ONE_UL: 1,
    CassetteType.FIVE_UL: 2,
    CassetteType.TEN_UL: 3,
}
CASSETTE_TYPES_BY_CODE = {value: key for key, value in CASSETTE_CODES.items()}
CASSETTE_VOLUME_RANGES_UL = {
    CassetteType.ONE_UL: (1, 50),
    CassetteType.FIVE_UL: (5, 2500),
    CassetteType.TEN_UL: (10, 3000),
}


def validate_volume_for_cassette(volume_ul: int, cassette_type: CassetteType) -> None:
    """Validate manual limits and full cassette-size increments."""

    if cassette_type is CassetteType.ANY:
        if not 1 <= volume_ul <= 3000:
            raise ValueError("volume must be between 1 and 3000 uL")
        return
    minimum, maximum = CASSETTE_VOLUME_RANGES_UL[cassette_type]
    if not minimum <= volume_ul <= maximum:
        raise ValueError(
            f"volume must be between {minimum} and {maximum} uL for {cassette_type.value}"
        )
    increment = int(cassette_type.value.removesuffix("ul"))
    if volume_ul % increment:
        raise ValueError(
            f"volume must be a full {increment} uL increment for {cassette_type.value}"
        )


class PeristalticDispense(BaseModel):
    """One full-plate dispense using the proven 96-deep-well geometry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["peristaltic_dispense"] = "peristaltic_dispense"
    plate_type: Literal["96_deep_well"] = "96_deep_well"
    pump: Literal["primary"] = "primary"
    volume_ul: int = Field(ge=1, le=3000)
    flow_rate: FlowRate = FlowRate.MEDIUM
    cassette_type: CassetteType = CassetteType.ANY
    pre_dispense_volume_ul: int = Field(default=10, ge=0, le=3000)
    pre_dispense_cycles: int = Field(default=2, ge=0, le=255)

    @model_validator(mode="after")
    def validate_dispense(self) -> "PeristalticDispense":
        validate_volume_for_cassette(self.volume_ul, self.cassette_type)
        if (self.pre_dispense_volume_ul == 0) != (self.pre_dispense_cycles == 0):
            raise ValueError(
                "pre-dispense volume and cycles must either both be zero or both be positive"
            )
        if self.pre_dispense_volume_ul:
            validate_volume_for_cassette(
                self.pre_dispense_volume_ul,
                self.cassette_type,
            )
        return self


class Protocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    steps: list[PeristalticDispense] = Field(min_length=1, max_length=100)

