"""
GDAL/PROJ 환경 정리 — rasterio import 전에 반드시 먼저 import 할 것.

시스템에 다른 PROJ/GDAL(예: MapServer)이 설치되어 PROJ_LIB/GDAL_DATA 가
그쪽을 가리키면 proj.db 버전 충돌(CRSError)이 난다.
rasterio 가 번들한 proj_data / gdal_data 경로로 강제 재설정한다.
"""
import os
import glob


def _fix():
    try:
        import rasterio
    except Exception:
        return
    base = os.path.dirname(rasterio.__file__)

    proj_dir = os.path.join(base, "proj_data")
    if os.path.exists(os.path.join(proj_dir, "proj.db")):
        os.environ["PROJ_LIB"] = proj_dir
        os.environ["PROJ_DATA"] = proj_dir

    # gdal_data 위치 탐색
    for cand in (
        os.path.join(base, "gdal_data"),
        os.path.join(os.path.dirname(base), "rasterio", "gdal_data"),
    ):
        if os.path.isdir(cand) and glob.glob(os.path.join(cand, "*.csv")) or \
           (os.path.isdir(cand) and os.listdir(cand)):
            os.environ["GDAL_DATA"] = cand
            break


_fix()
