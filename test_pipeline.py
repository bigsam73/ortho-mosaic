"""엔드투엔드 처리 테스트 (서버 없이 코어 파이프라인)."""
from app import _bootstrap  # noqa
import time
import numpy as np
import rasterio
from app import ortho, kernels

kernels.warmup()

info = ortho.inspect("data/uploads/sample.tif")
print("입력:", info.width, "x", info.height, "bounds_wgs84:", info.bounds_wgs84)

# 빨간 블록 영역(126.976~126.986 lon, lat 상단)을 덮는 폴리곤 (WGS84)
# 빨간 블록: 픽셀 x 900~1600, y 800~1400 → 지리좌표로 대략 중앙 상단
feat = {
    "type": "Polygon",
    "coordinates": [[
        [126.979, 37.5625], [126.9825, 37.5625],
        [126.9825, 37.565], [126.979, 37.565], [126.979, 37.5625],
    ]],
}

for method in ["blur", "pixelate", "solid"]:
    out = f"data/outputs/test_{method}.tif"
    t = time.time()
    res = ortho.process(
        "data/uploads/sample.tif", out, [feat], method=method,
        blur_radius=10, blur_passes=3, pixel_block=24,
        fill_color=(0, 0, 0), feather=4,
    )
    dt = time.time() - t
    # 검증: 출력 열리는지 + 크기 유지
    with rasterio.open(out) as ds:
        assert ds.width == info.width and ds.height == info.height
        ov = ds.overviews(1)
    print(f"[{method}] {dt:.3f}s  blocks {res['blocks_modified']}/{res['blocks_total']}  overviews={ov}")

print("모든 테스트 통과")
