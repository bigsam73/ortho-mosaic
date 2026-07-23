"""테스트용 지오레퍼런싱 정사영상(COG) 생성."""
from app import _bootstrap  # noqa: F401
import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.enums import Resampling

W, H = 3000, 2400
# 서울 시청 인근 작은 영역 (WGS84 -> 여기선 UTM 52N 유사 좌표 대신 4326 사용)
minlon, minlat, maxlon, maxlat = 126.976, 37.560, 126.986, 37.568
transform = from_bounds(minlon, minlat, maxlon, maxlat, W, H)

# 합성 이미지: 그라디언트 + 격자 + 색 블록 (모자이크 확인용)
yy, xx = np.mgrid[0:H, 0:W]
r = ((xx / W) * 255).astype(np.uint8)
g = ((yy / H) * 255).astype(np.uint8)
b = (((xx + yy) % 256)).astype(np.uint8)

# 격자선
grid = ((xx % 200 < 3) | (yy % 200 < 3))
r[grid] = 255; g[grid] = 255; b[grid] = 255
# 눈에 띄는 색 블록 (모자이크 대상 확인용)
r[800:1400, 900:1600] = 220; g[800:1400, 900:1600] = 40; b[800:1400, 900:1600] = 40

data = np.stack([r, g, b])

profile = dict(
    driver="GTiff", width=W, height=H, count=3, dtype="uint8",
    crs="EPSG:4326", transform=transform,
    tiled=True, blockxsize=512, blockysize=512,
    compress="deflate", predictor=2, photometric="RGB",
)

out = "data/uploads/sample.tif"
with rasterio.open(out, "w", **profile) as ds:
    ds.write(data)
    ds.build_overviews([2, 4, 8, 16], Resampling.average)

print("생성됨:", out)
with rasterio.open(out) as ds:
    print("크기:", ds.width, ds.height, "밴드:", ds.count,
          "블록:", ds.block_shapes[0], "오버뷰:", ds.overviews(1))
