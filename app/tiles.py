"""
XYZ 타일 렌더러 — 정사영상을 Leaflet 웹 지도에 미리보기로 표시.

- WebMercator (EPSG:3857) XYZ 스킴.
- 요청된 z/x/y 타일의 지리적 bbox 를 계산 → rasterio WarpedVRT 로
  해당 영역만 256x256 로 재투영/리샘플 (오버뷰 자동 사용 → 빠름).
- PNG 로 인코딩하여 반환.
"""

from __future__ import annotations

from . import _bootstrap  # noqa: F401

import io
import math

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds

WEBMERC = "EPSG:3857"
TILE = 256
ORIGIN = 20037508.342789244  # 지구 반둘레 (m)


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """z/x/y 타일의 EPSG:3857 bbox (minx, miny, maxx, maxy)."""
    n = 2 ** z
    res = (2 * ORIGIN) / n
    minx = -ORIGIN + x * res
    maxx = -ORIGIN + (x + 1) * res
    maxy = ORIGIN - y * res
    miny = ORIGIN - (y + 1) * res
    return (minx, miny, maxx, maxy)


def render_tile(path: str, z: int, x: int, y: int) -> bytes | None:
    """해당 타일 PNG 바이트 반환. 데이터 없으면 None."""
    minx, miny, maxx, maxy = tile_bounds_3857(z, x, y)

    with rasterio.open(path) as ds:
        # 데이터셋 범위를 3857 로 변환하여 타일과 교차 검사
        try:
            db = transform_bounds(ds.crs, WEBMERC, *ds.bounds, densify_pts=21)
        except Exception:
            db = ds.bounds
        if db[2] < minx or db[0] > maxx or db[3] < miny or db[1] > maxy:
            return None  # 겹치지 않음

        dst_transform = rasterio.transform.from_bounds(
            minx, miny, maxx, maxy, TILE, TILE
        )
        vrt_opts = dict(
            crs=WEBMERC,
            transform=dst_transform,
            width=TILE,
            height=TILE,
            resampling=Resampling.bilinear,
        )
        with WarpedVRT(ds, **vrt_opts) as vrt:
            data = vrt.read()  # (C, 256, 256)

    count = data.shape[0]
    if count >= 3:
        rgb = data[:3]
    else:
        rgb = np.repeat(data[:1], 3, axis=0)

    # 알파: 유효 데이터 마스크
    if count == 4:
        alpha = data[3]
    else:
        alpha = np.where(rgb.sum(axis=0) > 0, 255, 0).astype(np.uint8)

    img = np.concatenate([rgb, alpha[np.newaxis]], axis=0)  # (4,256,256)
    img = np.transpose(img, (1, 2, 0)).astype(np.uint8)      # (256,256,4)

    import cv2
    # OpenCV 는 BGRA
    bgra = cv2.cvtColor(img, cv2.COLOR_RGBA2BGRA)
    ok, buf = cv2.imencode(".png", bgra)
    if not ok:
        return None
    return buf.tobytes()
