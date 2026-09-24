## 1. 处理流程

```text
图片路径
  -> OpenCV 读取图片
  -> preprocess.steps 按 YAML 从上到下执行
  -> PP-OCRv6_medium_det + PP-OCRv6_medium_rec
  -> 过滤 ROI 边缘残缺文字
  -> 结果聚成 block
  -> ROI 坐标映射回整张原图
  -> JSON 返回
```

## 3. 有序预处理

```yaml
preprocess:
  enabled: true
  steps:
    - type: grayscale
      enabled: true

    - type: resize
      enabled: false
      scale: 1.25
      interpolation: cubic

    - type: gaussian_blur
      enabled: false
      kernel_size: 3
      sigma: 0.0

    - type: contrast_stretch
      enabled: false
      low_percentile: 1.0
      high_percentile: 99.0

    - type: clahe
      enabled: false
      clip_limit: 2.0
      tile_grid_size: 8

    - type: sharpen
      enabled: false
      strength: 0.4
      sigma: 1.0
```

这里的顺序就是实际处理顺序。如果改成：

```yaml
- type: sharpen
  enabled: true

- type: clahe
  enabled: true
```

程序就会**先 sharpen，再 CLAHE**。

支持的步骤：

```text
grayscale
resize
gamma
gaussian_blur
median_blur
bilateral_filter
contrast_stretch
clahe
sharpen
threshold       # fixed / otsu / adaptive
morphology      # open / close
invert
```

不建议一次全部开启。对当前皮带图


## 4. 调试图

开启：

```yaml
preprocess:
  debug:
    save_images: true
    output_dir: ./debug
```

如果实际启用顺序是：

```text
grayscale -> resize -> gaussian_blur
```

会保存：

```text
xxx_00_roi_original.png
xxx_01_grayscale.png
xxx_02_resize.png
xxx_03_gaussian_blur.png
```

数字前缀就是实际执行顺序。禁用的步骤不会执行，不会生成重复图片。

## 5. 安装

```bash
conda create -n beltocr python=3.10 -y
conda activate beltocr
```

```bash
python -m pip install -e .
```

PP-OCRv6 需要 `paddleocr>=3.7`。


## 6. 设置固定 ROI

工业相机位置固定后，可以用：

```bash
python scripts/select_roi.py --image /data/images/reference.png --config config.yaml
```

```yaml
roi:
  enabled: true
  reference_width: 1000
  reference_height: 750
  resize_to_reference: true
  scale_if_size_changes: false
```

程序会先把整张输入图缩放到 `1000×750`，再按固定 ROI 裁剪。返回的
`bbox` 仍使用原始输入图片坐标。输入图宽高比不同时会发生拉伸，可能影响
OCR 准确率。


## 7. 启动服务

```bash
conda activate beltocr
python -m belt_ocr.serve --config config.yaml
```

默认：

```text
http://127.0.0.1:8000
```


## 8. OCR 调用方式

只有这一种上层调用方式：

```python
import requests

response = requests.post(
    "http://127.0.0.1:8000/ocr",
    json={
        "image_path": "/data/images/001.png"
    },
    timeout=30
)

result = response.json()

print(result)
```


请求 JSON 只允许：

```json
{
  "image_path": "/data/images/001.png"
}
```



返回示例：

```json
{
  "image_path": "/data/images/001.png",
  "blocks": [
    {
      "bbox": [520, 240, 890, 510],
      "lines": [
        {
          "text": "2926 215C",
          "bbox": [530, 280, 820, 320],
          "confidence": 0.97
        }
      ]
    }
  ]
}
```

## 9. 边缘残缺文字过滤

```yaml
filters:
  discard_edge_lines: true
  edge_margin_px: 3
  edge_sides: [left, right, top, bottom]
```

OCR 行框触碰 ROI 边缘时，可作为“文字没有完整进入画面”的候选直接丢弃。
