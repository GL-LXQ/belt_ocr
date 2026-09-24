from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .config import load_config


def main():
    parser = argparse.ArgumentParser(description="启动皮带字符识别服务")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    app = create_app(config=cfg)
    # 只开一个 worker，不然每个进程都会在显存里再放一份模型。
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level=cfg.server.log_level, workers=1)


if __name__ == "__main__":
    main()
