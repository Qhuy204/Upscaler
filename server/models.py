from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from upscale_core.ffmpeg import PLATFORMS, parse_aspect


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")

    short: int | None = Field(None, ge=144, le=4320, description="target short side (default 1080)")
    to: str | None = Field(None, pattern=r"^\d{2,5}x\d{2,5}$", description="exact WxH, letterboxed")
    scale: float | None = Field(None, gt=0.1, le=4)
    strength: float = Field(1.0, ge=0, le=2, description="deblock/denoise/sharpen strength")
    quality: Literal["delivery", "master"] = "delivery"
    crf: int | None = Field(None, ge=10, le=40, description="override the preset's cq/crf")
    fit: Literal["auto", "pad", "crop", "stretch"] = Field(
        "auto", description="how the picture fills the frame; auto fills without bars when the aspect is close")
    aspect: str | None = Field(None, pattern=r"^\d{1,2}:\d{1,2}$",
                               description="force this output aspect, e.g. 9:16 (default: nearest the platform allows)")
    platform: Literal["tiktok", "meta", "any"] = Field(
        "tiktok", description="output is forced to an aspect this platform accepts (tiktok: 9:16, 16:9, 1:1)")
    snap_aspect: bool = Field(True, description="platform any: near-standard inputs (1920x1033) get 1920x1080")
    remove_borders: bool = Field(True, description="cut black bars baked into the input first")

    @model_validator(mode="after")
    def one_size(self):
        if sum(x is not None for x in (self.short, self.to, self.scale)) > 1:
            raise ValueError("give only one of short, to, scale")
        if self.aspect:
            if self.to:
                raise ValueError("aspect cannot be combined with to (to already sets the exact size)")
            a = parse_aspect(self.aspect)
            if 0 in a or not 0.2 <= a[0] / a[1] <= 5:
                raise ValueError("aspect must be between 1:5 and 5:1")
            allowed = PLATFORMS.get(self.platform)
            if allowed and a not in allowed:
                raise ValueError(f"aspect {self.aspect} is not accepted by platform {self.platform} "
                                 f"(allowed: {', '.join(f'{x}:{y}' for x, y in allowed)}; use platform any)")
        return self


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str | None = Field(None, max_length=4096)
    upload_key: str | None = Field(None, max_length=512)
    options: Options = Field(default_factory=Options)
    callback_url: str | None = Field(None, max_length=2048)
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_source(self):
        if (self.source_url is None) == (self.upload_key is None):
            raise ValueError("give exactly one of source_url, upload_key")
        return self


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_urls: list[str] | None = Field(None, min_length=1)
    drive_folder_url: str | None = Field(None, max_length=4096)
    options: Options = Field(default_factory=Options)
    callback_url: str | None = Field(None, max_length=2048)
    idempotency_key: str | None = Field(None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def one_source(self):
        if (self.source_urls is None) == (self.drive_folder_url is None):
            raise ValueError("give exactly one of source_urls, drive_folder_url")
        return self


class UploadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str | None = Field(None, max_length=255)
    content_type: str | None = Field(None, max_length=100)
