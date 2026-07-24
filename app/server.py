"""
FastAPI 백엔드 — 정사영상 모자이크 웹 플랫폼.

엔드포인트:
  POST /api/upload              정사영상 업로드 → id 발급
  GET  /api/ortho/{id}          메타데이터 (bounds 등)
  GET  /api/tiles/{id}/{z}/{x}/{y}.png   미리보기 타일
  POST /api/process             모자이크 처리 (job 시작)
  GET  /api/job/{job_id}        처리 진행률
  GET  /api/download/{id}       결과 정사영상 다운로드
  GET  /                        웹 UI
"""

from __future__ import annotations

from . import _bootstrap  # noqa: F401  (rasterio 관련 import 전 PROJ/GDAL 교정)

import os
import shutil
import threading
import uuid
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Body
from fastapi.responses import Response, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ortho, tiles, kernels

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE, "data", "uploads")
OUTPUT_DIR = os.path.join(BASE, "data", "outputs")
STATIC_DIR = os.path.join(BASE, "static")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Ortho Mosaic Platform")

# 메모리 레지스트리 (프로덕션에선 DB 권장)
ORTHOS: dict[str, dict] = {}   # id -> {path, output, info}
JOBS: dict[str, dict] = {}     # job_id -> {done, total, status, error}


@app.on_event("startup")
def _startup():
    # Numba JIT 미리 컴파일 (첫 요청 지연 제거)
    kernels.warmup()


# ---------------------------------------------------------------------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    oid = uuid.uuid4().hex[:12]
    ext = os.path.splitext(file.filename or "")[1] or ".tif"
    dst = os.path.join(UPLOAD_DIR, f"{oid}{ext}")
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f, length=1024 * 1024 * 8)

    try:
        info = ortho.inspect(dst)
    except Exception as e:
        os.remove(dst)
        raise HTTPException(400, f"정사영상을 열 수 없습니다: {e}")

    ORTHOS[oid] = {"path": dst, "output": None, "info": info}
    return {
        "id": oid,
        "filename": file.filename,
        "width": info.width,
        "height": info.height,
        "count": info.count,
        "dtype": info.dtype,
        "crs": info.crs,
        "bounds_wgs84": info.bounds_wgs84,
        "block_shape": info.block_shape,
        "overviews": info.overviews,
    }


@app.get("/api/ortho/{oid}")
def get_ortho(oid: str):
    rec = ORTHOS.get(oid)
    if not rec:
        raise HTTPException(404, "not found")
    info = rec["info"]
    return {
        "id": oid,
        "bounds_wgs84": info.bounds_wgs84,
        "width": info.width,
        "height": info.height,
        "processed": rec["output"] is not None,
    }


# ---------------------------------------------------------------------------
@app.get("/api/tiles/{oid}/{z}/{x}/{y}.png")
def get_tile(oid: str, z: int, x: int, y: int, source: str = "auto"):
    rec = ORTHOS.get(oid)
    if not rec:
        raise HTTPException(404, "not found")
    # source: 'original' | 'processed' | 'auto'(처리본 있으면 처리본)
    if source == "processed" and rec["output"]:
        path = rec["output"]
    elif source == "original":
        path = rec["path"]
    else:
        path = rec["output"] or rec["path"]

    png = tiles.render_tile(path, z, x, y)
    if png is None:
        return Response(status_code=204)
    return Response(content=png, media_type="image/png")


# ---------------------------------------------------------------------------
class ProcessReq(BaseModel):
    id: str
    features: list          # GeoJSON geometry 리스트 (WGS84)
    method: str = "blur"    # blur | pixelate | solid
    blur_radius: int = 8
    blur_passes: int = 3
    pixel_block: int = 16
    fill_color: list[int] = [0, 0, 0]
    feather: int = 0
    rebuild_overviews: bool = False   # True: 저해상도 미리보기 정합↑ / 저장 느림


@app.post("/api/process")
def process(req: ProcessReq):
    rec = ORTHOS.get(req.id)
    if not rec:
        raise HTTPException(404, "not found")
    if not req.features:
        raise HTTPException(400, "선택된 영역이 없습니다")

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"done": 0, "total": 0, "status": "running", "error": None}

    out_path = os.path.join(OUTPUT_DIR, f"{req.id}_mosaic.tif")

    def _run():
        try:
            def prog(done, total):
                JOBS[job_id]["done"] = done
                JOBS[job_id]["total"] = total

            result = ortho.process(
                rec["path"], out_path, req.features, method=req.method,
                blur_radius=req.blur_radius, blur_passes=req.blur_passes,
                pixel_block=req.pixel_block,
                fill_color=tuple(req.fill_color), feather=req.feather,
                rebuild_overviews=req.rebuild_overviews,
                progress=prog,
            )
            rec["output"] = out_path
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["result"] = result
        except Exception as e:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return job


# ---------------------------------------------------------------------------
@app.get("/api/download/{oid}")
def download(oid: str):
    rec = ORTHOS.get(oid)
    if not rec or not rec["output"]:
        raise HTTPException(404, "처리된 결과가 없습니다")
    return FileResponse(
        rec["output"],
        media_type="image/tiff",
        filename=f"{oid}_mosaic.tif",
    )


# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
