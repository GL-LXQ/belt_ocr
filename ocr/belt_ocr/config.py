from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class OCRConfig(BaseModel):
    device: str = "gpu:0"
    detection_model: str = "PP-OCRv6_medium_det"
    recognition_model: str = "PP-OCRv6_medium_rec"
    detection_model_dir: Optional[str] = None
    recognition_model_dir: Optional[str] = None
    engine: Optional[str] = None
    enable_hpi: bool = False
    recognition_batch_size: int = 8
    text_det_limit_side_len: Optional[int] = 2560
    text_det_limit_type: Optional[str] = "max"
    text_det_thresh: Optional[float] = 0.25
    text_det_box_thresh: Optional[float] = 0.45
    text_det_unclip_ratio: Optional[float] = 1.5


class ROIConfig(BaseModel):
    enabled: bool = False
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    reference_width: Optional[int] = None
    reference_height: Optional[int] = None
    resize_to_reference: bool = False
    scale_if_size_changes: bool = False

    @model_validator(mode="after")
    def validate_roi(self):
        if self.enabled and (self.w <= 0 or self.h <= 0):
            raise ValueError("ROI enabled 时 w/h 必须大于 0")
        if self.resize_to_reference and (
            not self.reference_width
            or not self.reference_height
            or self.reference_width <= 0
            or self.reference_height <= 0
        ):
            raise ValueError(
                "resize_to_reference=true 时 reference_width/reference_height 必须大于 0"
            )
        return self


class StepBase(BaseModel):
    enabled: bool = True


class GrayscaleStep(StepBase):
    type: Literal["grayscale"] = "grayscale"


class ResizeStep(StepBase):
    type: Literal["resize"] = "resize"
    scale: float = 2.0
    interpolation: Literal["nearest", "linear", "cubic", "area", "lanczos4"] = "cubic"

    @field_validator("scale")
    @classmethod
    def validate_scale(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("resize.scale 必须大于 0")
        return value


class GammaStep(StepBase):
    type: Literal["gamma"] = "gamma"
    gamma: float = 1.0

    @field_validator("gamma")
    @classmethod
    def validate_gamma(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("gamma.gamma 必须大于 0")
        return value


class GaussianBlurStep(StepBase):
    type: Literal["gaussian_blur"] = "gaussian_blur"
    kernel_size: int = 3
    sigma: float = 0.0

    @field_validator("kernel_size")
    @classmethod
    def validate_kernel_size(cls, value: int) -> int:
        if value < 3 or value % 2 == 0:
            raise ValueError("gaussian_blur.kernel_size 必须是 >=3 的奇数")
        return value

    @field_validator("sigma")
    @classmethod
    def validate_sigma(cls, value: float) -> float:
        if value < 0:
            raise ValueError("gaussian_blur.sigma 不能小于 0")
        return value


class MedianBlurStep(StepBase):
    type: Literal["median_blur"] = "median_blur"
    kernel_size: int = 3

    @field_validator("kernel_size")
    @classmethod
    def validate_kernel_size(cls, value: int) -> int:
        if value < 3 or value % 2 == 0:
            raise ValueError("median_blur.kernel_size 必须是 >=3 的奇数")
        return value


class BilateralFilterStep(StepBase):
    type: Literal["bilateral_filter"] = "bilateral_filter"
    diameter: int = 5
    sigma_color: float = 25.0
    sigma_space: float = 25.0

    @field_validator("diameter")
    @classmethod
    def validate_diameter(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("bilateral_filter.diameter 必须大于 0")
        return value

    @field_validator("sigma_color", "sigma_space")
    @classmethod
    def validate_sigmas(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("bilateral_filter sigma 必须大于 0")
        return value


class ContrastStretchStep(StepBase):
    type: Literal["contrast_stretch"] = "contrast_stretch"
    low_percentile: float = 1.0
    high_percentile: float = 99.0

    @model_validator(mode="after")
    def validate_percentiles(self):
        if not 0 <= self.low_percentile < self.high_percentile <= 100:
            raise ValueError("contrast_stretch 需要 0 <= low_percentile < high_percentile <= 100")
        return self


class CLAHEStep(StepBase):
    type: Literal["clahe"] = "clahe"
    clip_limit: float = 2.0
    tile_grid_size: int = 8

    @field_validator("clip_limit")
    @classmethod
    def validate_clip_limit(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("clahe.clip_limit 必须大于 0")
        return value

    @field_validator("tile_grid_size")
    @classmethod
    def validate_tile_grid_size(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("clahe.tile_grid_size 必须大于 0")
        return value


class SharpenStep(StepBase):
    type: Literal["sharpen"] = "sharpen"
    strength: float = 0.4
    sigma: float = 1.0

    @field_validator("strength")
    @classmethod
    def validate_strength(cls, value: float) -> float:
        if value < 0:
            raise ValueError("sharpen.strength 不能小于 0")
        return value

    @field_validator("sigma")
    @classmethod
    def validate_sigma(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("sharpen.sigma 必须大于 0")
        return value


class ThresholdStep(StepBase):
    type: Literal["threshold"] = "threshold"
    method: Literal["fixed", "otsu", "adaptive"] = "otsu"
    value: int = 150
    invert: bool = False
    adaptive_block_size: int = 31
    adaptive_c: float = 5.0

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: int) -> int:
        if not 0 <= value <= 255:
            raise ValueError("threshold.value 必须在 0~255")
        return value

    @field_validator("adaptive_block_size")
    @classmethod
    def validate_adaptive_block_size(cls, value: int) -> int:
        if value < 3 or value % 2 == 0:
            raise ValueError("threshold.adaptive_block_size 必须是 >=3 的奇数")
        return value

class TophatStep(StepBase):
    type: Literal["tophat"] = "tophat"
    kernel_size: int = 15
    shape: Literal["rect", "ellipse", "cross"] = "rect"

    @field_validator("kernel_size")
    @classmethod
    def validate_kernel_size(cls, value: int) -> int:
        if value <= 0 or value % 2 == 0:
            raise ValueError("tophat.kernel_size 必须是正奇数")
        return value

class RemoveHorizontalLinesStep(StepBase):
    type: Literal["remove_horizontal_lines"] = "remove_horizontal_lines"
    line_length: int = 61
    line_height: int = 1
    vertical_dilate: int = 3
    strength: float = 2.0

    @field_validator("line_length", "line_height", "vertical_dilate")
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("remove_horizontal_lines 的尺寸参数必须大于 0")
        return value

    @field_validator("strength")
    @classmethod
    def validate_strength(cls, value: float) -> float:
        if value < 0:
            raise ValueError("remove_horizontal_lines.strength 不能小于 0")
        return value

class MorphologyStep(StepBase):
    type: Literal["morphology"] = "morphology"
    operation: Literal["open", "close"] = "close"
    kernel_size: int = 3
    iterations: int = 1

    @field_validator("kernel_size")
    @classmethod
    def validate_kernel_size(cls, value: int) -> int:
        if value <= 0 or value % 2 == 0:
            raise ValueError("morphology.kernel_size 必须是正奇数")
        return value

    @field_validator("iterations")
    @classmethod
    def validate_iterations(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("morphology.iterations 必须大于 0")
        return value


class InvertStep(StepBase):
    type: Literal["invert"] = "invert"


PreprocessStep = Annotated[
    Union[
        GrayscaleStep,
        ResizeStep,
        GammaStep,
        GaussianBlurStep,
        MedianBlurStep,
        BilateralFilterStep,
        ContrastStretchStep,
        CLAHEStep,
        TophatStep,
        SharpenStep,
        ThresholdStep,
        MorphologyStep,
        InvertStep,
        RemoveHorizontalLinesStep,
    ],
    Field(discriminator="type"),
]


class DebugConfig(BaseModel):
    save_images: bool = False
    output_dir: str = "./debug"


class PreprocessConfig(BaseModel):
    enabled: bool = True
    steps: List[PreprocessStep] = Field(default_factory=lambda: [GrayscaleStep(enabled=True)])
    debug: DebugConfig = Field(default_factory=DebugConfig)


class GroupingConfig(BaseModel):
    horizontal_overlap_ratio: float = 0.30
    center_x_tolerance_ratio: float = 0.45
    max_vertical_gap_ratio: float = 1.8
    min_lines_per_block: int = 1


class FilterConfig(BaseModel):
    min_confidence: float = 0.0
    discard_edge_lines: bool = True
    edge_margin_px: int = 3
    edge_sides: List[str] = Field(default_factory=lambda: ["left", "right", "top", "bottom"])
    strip_text: bool = True


class OutputConfig(BaseModel):
    save_json: bool = True
    output_dir: str = "./output"
    ensure_ascii: bool = False
    indent: int = 2


class SecurityConfig(BaseModel):
    allowed_roots: List[str] = Field(default_factory=list)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"


class AppConfig(BaseModel):
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    roi: ROIConfig = Field(default_factory=ROIConfig)
    preprocess: PreprocessConfig = Field(default_factory=PreprocessConfig)
    grouping: GroupingConfig = Field(default_factory=GroupingConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
