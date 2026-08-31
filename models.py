"""Strict typed models for supported base MultiFlo protocol steps."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FlowRate(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CassetteType(str, Enum):
    ANY = "any"
    ONE_UL = "1ul"
    FIVE_UL = "5ul"
    TEN_UL = "10ul"


class PlateType(str, Enum):
    WELL_384 = "384_well"
    WELL_96 = "96_well"
    DEEP_WELL_96 = "96_deep_well"


PLATE_COLUMN_COUNTS = {
    PlateType.WELL_384: 24,
    PlateType.WELL_96: 12,
    PlateType.DEEP_WELL_96: 12,
}

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
    """One primary peristaltic dispense to a supported plate geometry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["peristaltic_dispense"] = "peristaltic_dispense"
    plate_type: PlateType = PlateType.DEEP_WELL_96
    pump: Literal["primary"] = "primary"
    volume_ul: int = Field(ge=1, le=3000)
    flow_rate: FlowRate = FlowRate.MEDIUM
    cassette_type: CassetteType = CassetteType.ANY
    pre_dispense_volume_ul: int = Field(default=10, ge=0, le=3000)
    pre_dispense_cycles: int = Field(default=2, ge=0, le=255)
    columns: Literal["all"] | tuple[int, ...] = "all"
    row_sections: Literal["all", "odd", "even"] = "all"
    x_offset_steps: int = Field(default=0, ge=-60, le=60)
    y_offset_steps: int = Field(default=0, ge=-40, le=40)

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
        if self.columns != "all":
            if not self.columns:
                raise ValueError("at least one dispense column is required")
            highest = PLATE_COLUMN_COUNTS[self.plate_type]
            if any(column < 1 or column > highest for column in self.columns):
                raise ValueError(
                    f"{self.plate_type.value} columns must be between 1 and {highest}"
                )
            if len(set(self.columns)) != len(self.columns):
                raise ValueError("dispense columns must be unique")
        if (
            self.row_sections != "all"
            and self.plate_type is not PlateType.WELL_384
        ):
            raise ValueError("row-section selection is supported only for 384-well plates")
        return self


class PeristalticPrime(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["peristaltic_prime"] = "peristaltic_prime"
    plate_type: PlateType = PlateType.WELL_96
    pump: Literal["primary"] = "primary"
    volume_ul: int = Field(ge=1, le=3000)
    flow_rate: FlowRate = FlowRate.MEDIUM
    cassette_type: CassetteType = CassetteType.ANY


class PeristalticPurge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["peristaltic_purge"] = "peristaltic_purge"
    plate_type: PlateType = PlateType.WELL_96
    pump: Literal["primary"] = "primary"
    volume_ul: int = Field(ge=1, le=3000)
    flow_rate: FlowRate = FlowRate.MEDIUM
    cassette_type: CassetteType = CassetteType.ANY


class Shake(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["shake"] = "shake"
    plate_type: PlateType = PlateType.WELL_96
    duration_seconds: int = Field(ge=1, le=60)
    move_carrier_home: bool = True
    axis: Literal["x"] = "x"
    speed: Literal["medium"] = "medium"


class Soak(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["soak"] = "soak"
    plate_type: PlateType = PlateType.WELL_96
    duration_seconds: int = Field(ge=1, le=60)
    move_carrier_home: bool = True


ProtocolStep = Annotated[
    PeristalticDispense | PeristalticPrime | PeristalticPurge | Shake | Soak,
    Field(discriminator="operation"),
]


class Protocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    steps: list[ProtocolStep] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_single_plate_geometry(self) -> "Protocol":
        """Reject a protocol that mixes plate types.

        Each step sends its own plate selector in Start Batch, and plate type
        selects the dispense height. The step defaults differ (dispense is
        96-deep-well, everything else is 96-well), so a protocol that leaves
        plate_type unset on some steps would silently run two geometries
        against one physical plate.
        """

        plate_types = {step.plate_type for step in self.steps}
        if len(plate_types) > 1:
            named = ", ".join(sorted(plate.value for plate in plate_types))
            raise ValueError(
                f"every step must use the same plate type; got {named}. Set "
                "plate_type explicitly on each step."
            )
        return self
