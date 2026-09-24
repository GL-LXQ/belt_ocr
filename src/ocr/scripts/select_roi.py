#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import cv2
import yaml


def main():
    parser = argparse.ArgumentParser(description="在参考图片上框选皮带区域，并写入配置文件")
    parser.add_argument("--image", required=True, help="参考图片路径")
    parser.add_argument("--config", default="config.yaml", help="需要更新的配置文件")
    args = parser.parse_args()

    image_path = Path(args.image).expanduser().resolve()
    img = cv2.imread(str(image_path), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise SystemExit(f"图片读取失败：{image_path}")

    if img.ndim == 2:
        display = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    else:
        display = img

    x, y, w, h = cv2.selectROI(
        "框选皮带区域 - 回车或空格确认，C 取消",
        display,
        showCrosshair=True,
        fromCenter=False,
    )
    cv2.destroyAllWindows()
    if w <= 0 or h <= 0:
        raise SystemExit("没有选中区域，已取消")

    cfg_path = Path(args.config)
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    height, width = img.shape[:2]
    data["roi"] = {
        "enabled": True,
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "reference_width": int(width),
        "reference_height": int(height),
        "resize_to_reference": True,
        "scale_if_size_changes": False,
    }
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"ROI 已写入 {cfg_path}：x={x}, y={y}, w={w}, h={h}，图片尺寸={width}x{height}")


if __name__ == "__main__":
    main()
