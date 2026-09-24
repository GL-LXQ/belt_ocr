import sys
import json
import time
import requests
from pathlib import Path


image_path = Path(sys.argv[1]).expanduser().resolve()

start = time.perf_counter()

response = requests.post(
    "http://127.0.0.1:8000/ocr",
    json={
        "image_path": str(image_path)
    },
    timeout=30
)

elapsed = time.perf_counter() - start

result = response.json()

print(
    json.dumps(
        result,
        ensure_ascii=False,
        indent=4
    )
)

print(f"\n总耗时: {elapsed:.3f} 秒")
print(f"总耗时: {elapsed * 1000:.1f} ms")
