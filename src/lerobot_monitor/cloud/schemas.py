"""Version 1 cloud API inputs. Tensor arrays are finite, bounded JSON values."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DeploymentCreate(Input):
    name: str = Field(min_length=1, max_length=200)
    source_kind: Literal["huggingface", "path"]
    source: str = Field(min_length=1, max_length=4096)
    revision: str | None = Field(default=None, max_length=200)
    owned_upload: bool = False


class LoadRequest(Input):
    gpu_uuid: str = Field(min_length=1, max_length=128)
    device: Literal["cuda", "cpu"] = "cuda"


class SessionOpen(Input):
    mode: Literal["select_action", "debug_chunk", "rtc_chunk"] = "select_action"
    task: str = Field(default="", max_length=10000)
    state_keys: list[str] | None = Field(default=None, min_length=1, max_length=256)
    overrides: dict[str, str] = Field(default_factory=dict, max_length=64)

    @field_validator("state_keys")
    @classmethod
    def unique_keys(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (len(set(value)) != len(value) or any(not key for key in value)):
            raise ValueError("state_keys must be non-empty and unique")
        return value


class SessionEpoch(Input):
    epoch: int = Field(ge=0)


class InferRequest(SessionEpoch):
    request_id: str = Field(min_length=1, max_length=128)
    state: dict[str, float] = Field(min_length=1, max_length=256)
    images: dict[str, str] = Field(default_factory=dict, max_length=16)
    task: str | None = Field(default=None, max_length=10000)
    chunk_size: int = Field(default=32, ge=1, le=1024)
    prefix_raw: list[list[float]] | None = Field(default=None, max_length=1024)
    prefix_absolute: list[list[float]] | None = Field(default=None, max_length=1024)
    inference_delay: int = Field(default=0, ge=0, le=1024)

    @field_validator("images")
    @classmethod
    def bounded_images(cls, value: dict[str, str]) -> dict[str, str]:
        if sum(len(encoded) for encoded in value.values()) > 32 * 1024 * 1024:
            raise ValueError("encoded images exceed 32 MiB")
        return value

    @model_validator(mode="after")
    def valid_prefix(self) -> InferRequest:
        if (self.prefix_raw is None) != (self.prefix_absolute is None):
            raise ValueError("raw and absolute prefixes must be provided together")
        for rows in (self.prefix_raw, self.prefix_absolute):
            if rows is not None:
                if not rows or not rows[0] or len(rows[0]) > 256:
                    raise ValueError("prefix must have shape [T,A], 1 <= A <= 256")
                if any(len(row) != len(rows[0]) or not all(math.isfinite(x) for x in row) for row in rows):
                    raise ValueError("prefix must be rectangular and finite")
        if self.prefix_raw is not None and self.prefix_absolute is not None:
            if (len(self.prefix_raw), len(self.prefix_raw[0])) != (len(self.prefix_absolute), len(self.prefix_absolute[0])):
                raise ValueError("raw and absolute prefix shapes must match")
        return self
