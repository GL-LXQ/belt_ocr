from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from .config import AppConfig, load_config
from .engine import BeltOCREngine


class OCRRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_path: str


def create_app(
    config: Optional[AppConfig] = None,
    config_path: Optional[str] = None,
    backend_factory: Optional[Callable] = None,
) -> FastAPI:
    cfg = config or load_config(config_path or "config.yaml")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        backend = backend_factory(cfg.ocr) if backend_factory else None
        app.state.engine = BeltOCREngine(cfg, backend=backend)
        yield
        app.state.engine = None

    app = FastAPI(title="Belt OCR Service", version="0.3.0", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "device": cfg.ocr.device,
            "detection_model": cfg.ocr.detection_model,
            "recognition_model": cfg.ocr.recognition_model,
        }

    @app.post("/ocr")
    async def ocr(req: OCRRequest):
        try:
            return await run_in_threadpool(app.state.engine.process, req.image_path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"OCR 识别失败：{exc}") from exc

    return app
