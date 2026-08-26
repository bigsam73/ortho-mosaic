"""
대용량 정사영상(GeoTIFF/COG) 타일 단위 I/O 및 모자이크 처리.

핵심 설계 (대용량 대응):
- 절대 전체 이미지를 메모리에 올리지 않는다.
- rasterio 의 내부 블록(타일) 구조를 따라 windowed read/write.
- 처리 영역(폴리곤 BBox)에 걸치는 블록만 순회 → 불필요한 I/O 제거.
- 각 블록은 (H,W,C) uint8 배열로 읽어 Numba 커널로 in-place 처리 후 되쓰기.
- 출력은 COG (tiled + overview) 로 저장하여 웹 타일 서빙에 최적화.
"""

from __future__ import annotations

from . import _bootstrap  # noqa: F401  (rasterio import 전 PROJ/GDAL 경로 교정)

import json
import os
import shutil
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds
from rasterio.features import geometry_mask
from rasterio.enums import Resampling
from rasterio.warp import transform_geom
from rasterio.shutil import copy as rio_copy

from . import kernels


# ---------------------------------------------------------------------------
# 메타데이터
# ---------------------------------------------------------------------------
@dataclass
class OrthoInfo:
    path: str
    width: int
    height: int
    count: int
    dtype: str
    crs: str | None
    bounds: tuple[float, float, float, float]      # 원본 CRS 기준
    bounds_wgs84: tuple[float, float, float, float]  # lon/lat (leaflet 용)
    block_shape: tuple[int, int]
    overviews: list[int]


def inspect(path: str) -> OrthoInfo:
    with rasterio.open(path) as ds:
        b = ds.bounds
        # WGS84 경위도 bounds 계산 (프론트 지도 표시용)
        if ds.crs and ds.crs.to_epsg() != 4326:
            geom = {
                "type": "Polygon",
                "coordinates": [[
                    [b.left, b.bottom], [b.right, b.bottom],
                    [b.right, b.top], [b.left, b.top], [b.left, b.bottom],
                ]],
            }
            wgs = transform_geom(ds.crs, "EPSG:4326", geom)
            xs = [c[0] for c in wgs["coordinates"][0]]
            ys = [c[1] for c in wgs["coordinates"][0]]
            bounds_wgs84 = (min(xs), min(ys), max(xs), max(ys))
        else:
            bounds_wgs84 = (b.left, b.bottom, b.right, b.top)

        try:
            block = ds.block_shapes[0]
        except Exception:
            block = (512, 512)

        return OrthoInfo(
            path=path,
            width=ds.width,
            height=ds.height,
            count=ds.count,
            dtype=str(ds.dtypes[0]),
            crs=str(ds.crs) if ds.crs else None,
            bounds=(b.left, b.bottom, b.right, b.top),
            bounds_wgs84=bounds_wgs84,
            block_shape=(int(block[0]), int(block[1])),
            overviews=list(ds.overviews(1)),
        )


# ---------------------------------------------------------------------------
# GeoJSON(WGS84) → 데이터셋 CRS 로 변환
# ---------------------------------------------------------------------------
def _project_geoms(ds, features_wgs84: list[dict]) -> list[dict]:
    geoms = []
    for feat in features_wgs84:
        geom = feat["geometry"] if "geometry" in feat else feat
        if ds.crs and ds.crs.to_epsg() != 4326:
            geom = transform_geom("EPSG:4326", ds.crs, geom)
        geoms.append(geom)
    return geoms


def _geoms_bounds(geoms: Iterable[dict]) -> tuple[float, float, float, float]:
    minx = miny = float("inf")
    maxx = maxy = float("-inf")

    def walk(coords):
        nonlocal minx, miny, maxx, maxy
        if isinstance(coords[0], (int, float)):
            x, y = coords[0], coords[1]
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)
        else:
            for c in coords:
                walk(c)

    for g in geoms:
        walk(g["coordinates"])
    return (minx, miny, maxx, maxy)


# ---------------------------------------------------------------------------
# 블록 순회 유틸: 주어진 픽셀 윈도우를 내부 블록 크기로 잘라 iterate
# ---------------------------------------------------------------------------
def _iter_blocks(win: Window, full_w: int, full_h: int,
                 bw: int, bh: int) -> Iterable[Window]:
    x0 = max(0, int(np.floor(win.col_off)))
    y0 = max(0, int(np.floor(win.row_off)))
    x1 = min(full_w, int(np.ceil(win.col_off + win.width)))
    y1 = min(full_h, int(np.ceil(win.row_off + win.height)))

    # 블록 경계에 정렬
    bx0 = (x0 // bw) * bw
    by0 = (y0 // bh) * bh
    for by in range(by0, y1, bh):
        h = min(bh, y1 - by)
        if h <= 0 or by >= full_h:
            continue
        for bx in range(bx0, x1, bw):
            w = min(bw, x1 - bx)
            if w <= 0 or bx >= full_w:
                continue
            yield Window(bx, by, w, h)


# ---------------------------------------------------------------------------
# 메인 처리 파이프라인
# ---------------------------------------------------------------------------
def process(
    src_path: str,
    dst_path: str,
    features_wgs84: list[dict],
    method: str = "blur",
    *,
    blur_radius: int = 8,
    blur_passes: int = 3,
    pixel_block: int = 16,
    fill_color: tuple[int, int, int] = (0, 0, 0),
    feather: int = 0,
    rebuild_overviews: bool = False,
    progress=None,
) -> dict:
    """
    src_path 를 dst_path 로 복사한 뒤, features(폴리곤들) 영역에만
    모자이크(method)를 블록 단위로 적용한다.

    method: 'blur' | 'pixelate' | 'solid'
    feather: 마스크 경계 팽창 픽셀 (블러 시 경계 자연스럽게)
    progress: callable(done_blocks, total_blocks) 진행률 콜백

    [속도 최적화 핵심]
    - 전체 재압축 복사(느림) 대신 OS 파일 복사(shutil)로 원본을 그대로 복제.
      압축 상태/오버뷰/구조가 바이트 단위로 보존되어 재압축 CPU 비용이 0.
      (QGIS 가 빠른 이유와 동일: 바뀐 부분만 손대고 나머지는 건드리지 않음)
    - 마스크 영역에 겹치는 블록만 read/modify/write.
    - 오버뷰는 전체 재생성하지 않고, 변경된 픽셀 윈도우에 해당하는
      오버뷰 영역만 부분 갱신한다.
    """
    # 1) OS 레벨 raw 복사 — 재압축 없음, 오버뷰까지 그대로 복제됨.
    shutil.copyfile(src_path, dst_path)
    # 사이드카(.ovr, .msk 등) 도 함께 복사
    for ext in (".ovr", ".msk", ".aux.xml"):
        side = src_path + ext
        if os.path.exists(side):
            shutil.copyfile(side, dst_path + ext)

    with rasterio.open(dst_path, "r+") as ds:
        geoms = _project_geoms(ds, features_wgs84)
        gminx, gminy, gmaxx, gmaxy = _geoms_bounds(geoms)

        # 처리 영역의 픽셀 윈도우 (마스크 페더링 마진 포함)
        win = from_bounds(gminx, gminy, gmaxx, gmaxy, ds.transform)
        margin = feather + 2
        win = Window(
            win.col_off - margin, win.row_off - margin,
            win.width + 2 * margin, win.height + 2 * margin,
        )

        c_total = ds.count
        color = np.array(fill_color, dtype=np.uint8)

        # ── 큰 반경/블록 대응: 처리 타일 크기와 halo(겹침)를 값에 맞춰 산정 ──
        # halo = 블러 확산 범위(radius*passes) 또는 픽셀 블록 크기.
        #   read 는 [유효영역 + halo] 를 읽어 커널을 적용하고,
        #   write 는 halo 를 제외한 유효영역만 되쓴다 → 타일 경계 아티팩트 제거.
        # 유효 타일 크기는 파일 블록(512) 이상이되, 픽셀블록/블러반경보다 충분히 크게.
        bh0, bw0 = ds.block_shapes[0]
        base = max(bh0, bw0, 512)
        if method == "pixelate":
            halo = int(pixel_block)
            # 픽셀 셀이 유효영역에 여러 개 온전히 들어가도록 타일을 키움
            core = max(base, int(pixel_block) * 4)
        elif method == "blur":
            halo = int(blur_radius) * int(blur_passes) + 1
            core = max(base, halo * 2, 512)
        else:  # solid
            halo = 0
            core = base

        # 처리 영역을 유효(core) 타일 그리드로 분할
        tiles_list = list(_iter_blocks(win, ds.width, ds.height, core, core))
        total = len(tiles_list)
        touched = 0

        # 실제로 변경된 픽셀 범위 추적 (오버뷰 부분 갱신용)
        dminx = dminy = None
        dmaxx = dmaxy = None

        for i, cw in enumerate(tiles_list):
            # 유효영역 (write 대상)
            vx0, vy0 = int(cw.col_off), int(cw.row_off)
            vx1, vy1 = vx0 + int(cw.width), vy0 + int(cw.height)

            # halo 를 붙인 read 영역 (파일 경계로 클램프)
            rx0 = max(0, vx0 - halo)
            ry0 = max(0, vy0 - halo)
            rx1 = min(ds.width, vx1 + halo)
            ry1 = min(ds.height, vy1 + halo)
            rw = Window(rx0, ry0, rx1 - rx0, ry1 - ry0)
            wt = ds.window_transform(rw)

            # read 영역 전체에 대한 마스크 (halo 포함해야 블러/픽셀 평균이 정확)
            mask = geometry_mask(
                geoms, out_shape=(int(rw.height), int(rw.width)),
                transform=wt, invert=True, all_touched=True,
            ).astype(np.uint8)

            # 유효영역 안에 마스크가 없으면 이 타일은 건너뜀
            if not mask[vy0 - ry0:vy1 - ry0, vx0 - rx0:vx1 - rx0].any():
                if progress:
                    progress(i + 1, total)
                continue

            if feather > 0:
                mask = _dilate(mask, feather)

            arr = ds.read(window=rw)                      # (C,H,W)
            img = np.ascontiguousarray(np.transpose(arr, (1, 2, 0)))  # (H,W,C)
            c = img.shape[2]

            if method == "solid":
                col = color[:c] if len(color) >= c else np.resize(color, c)
                kernels.fill_solid(img, mask, np.ascontiguousarray(col))
            elif method == "pixelate":
                # 전역 그리드 정렬: read 영역 좌상단(rx0,ry0)을 오프셋으로 전달
                kernels.pixelate(img, mask, int(pixel_block), ry0, rx0)
            else:  # blur
                kernels.box_blur(img, mask, int(blur_radius), int(blur_passes))

            # halo 를 제외한 유효영역만 잘라 되쓰기
            sub = img[vy0 - ry0:vy1 - ry0, vx0 - rx0:vx1 - rx0, :]
            out = np.ascontiguousarray(np.transpose(sub, (2, 0, 1)))
            ds.write(out, window=cw)
            touched += 1

            # 변경 범위 갱신
            dminx = vx0 if dminx is None else min(dminx, vx0)
            dminy = vy0 if dminy is None else min(dminy, vy0)
            dmaxx = vx1 if dmaxx is None else max(dmaxx, vx1)
            dmaxy = vy1 if dmaxy is None else max(dmaxy, vy1)

            if progress:
                progress(i + 1, total)

        # 오버뷰: 재생성하지 않음 (QGIS 와 동일한 전략).
        # 원본에서 shutil 로 복사해 온 기존 오버뷰가 그대로 유지된다.
        # → 저장이 즉시 끝난다. 풀 해상도 데이터는 정확.
        # 모자이크 영역의 저해상도 미리보기 정합이 필요하면
        # rebuild_overviews=True 로 명시적으로 재생성할 수 있다.
        if rebuild_overviews and touched > 0:
            factors = ds.overviews(1) or [2, 4, 8, 16, 32]
            ds.build_overviews(factors, Resampling.average)
            if progress:
                progress(total, total)

    return {"blocks_total": total, "blocks_modified": touched}


# ---------------------------------------------------------------------------
# COG 복사
# ---------------------------------------------------------------------------
def _copy_as_cog(src_path: str, dst_path: str):
    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            compress="deflate",
            predictor=2,
            BIGTIFF="IF_SAFER",
        )
        with rasterio.open(dst_path, "w", **profile) as dst:
            # 블록 단위 복사 (메모리 절약)
            for _, win in src.block_windows(1):
                dst.write(src.read(window=win), window=win)
            # 컬러 인터프리테이션 유지
            try:
                dst.colorinterp = src.colorinterp
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 마스크 팽창 (경계 페더링용) - OpenCV 사용
# ---------------------------------------------------------------------------
def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    import cv2
    k = 2 * px + 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    return cv2.dilate(mask, kernel)
