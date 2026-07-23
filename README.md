# 정사영상 모자이크 웹 플랫폼

대용량 정사영상(GeoTIFF/COG)을 업로드하고, 지도 위에서 특정 지역을
선택하여 모자이크(블러/픽셀화/단색) 처리한 뒤 다시 정사영상(GeoTIFF)으로
저장하는 웹 플랫폼입니다.

## 속도 최적화 설계 ("인라인 어셈블러처럼 빠르게")

- **Numba `@njit(parallel=True, fastmath=True)`** — 픽셀 커널을 LLVM 네이티브
  코드로 컴파일하고, 멀티코어 + AVX/SSE SIMD 벡터화를 자동 적용합니다.
  (사실상 손으로 짠 어셈블러에 근접한 성능)
- **타일(블록) 단위 I/O** — 전체 이미지를 메모리에 올리지 않습니다.
  `rasterio` 의 windowed read/write 로 처리 영역에 걸치는 블록만 순회합니다.
  → 수 GB~수십 GB 정사영상도 메모리 사용량이 블록 크기 수준으로 일정.
- **처리 영역 프루닝** — 선택 폴리곤의 BBox 에 겹치는 블록만 읽고 씁니다.
- **박스 블러 O(N)** — 분리형(가로/세로) 슬라이딩 윈도우 누적합. 반경과
  무관하게 픽셀당 상수 시간. 3회 반복으로 가우시안 근사.
- **COG 출력** — tiled + overview 로 저장하여 웹 타일 서빙에 최적.

### 벤치마크 (16 MP 타일, 로컬)
| 처리 | 시간 |
|------|------|
| 단색 채우기 | 0.005 s |
| 픽셀화 | 0.019 s |
| 블러 (3-pass) | 0.558 s |

## 구성

```
ortho-mosaic/
  app/
    _bootstrap.py   # PROJ/GDAL 경로 교정 (rasterio import 전 필수)
    kernels.py      # Numba 가속 모자이크 커널 (블러/픽셀화/단색)
    ortho.py        # COG 타일 단위 I/O + 마스크 래스터라이징 + 처리 파이프라인
    tiles.py        # XYZ 웹 타일 렌더러 (WarpedVRT 재투영)
    server.py       # FastAPI 백엔드
  static/
    index.html      # Leaflet UI
    app.js          # 프론트엔드 로직 (업로드/영역 그리기/처리/다운로드)
  data/
    uploads/  outputs/
  run.py            # 실행 진입점
  make_sample.py    # 테스트용 샘플 정사영상 생성
  test_pipeline.py  # 코어 파이프라인 테스트
  test_api.py       # HTTP API 엔드투엔드 테스트
```

## 설치

```powershell
pip install -r requirements.txt
```

주요 의존성: fastapi, uvicorn, rasterio, numpy, numba, opencv-python

## 실행

```powershell
python run.py --host 127.0.0.1 --port 8000
```

브라우저에서 http://127.0.0.1:8000 접속.

### 사용 순서
1. **업로드** — GeoTIFF/COG 정사영상 선택 후 업로드. 지도에 미리보기 표시.
2. **영역 지정** — 지도 우측 상단 도구로 사각형/폴리곤을 그립니다 (여러 개 가능).
3. **처리 방식** — 블러 / 픽셀화 / 단색 중 선택하고 세부 옵션 조정.
4. **실행 & 저장** — "모자이크 처리 시작" → 진행률 표시 → 완료 후
   "결과 정사영상 다운로드". 원본/처리본 미리보기 토글 가능.

## 테스트

```powershell
python make_sample.py      # 샘플 생성 (data/uploads/sample.tif)
python test_pipeline.py    # 코어 파이프라인
# 서버 실행 후 별도 터미널에서:
python test_api.py         # HTTP API 흐름
```

## 참고 / 주의

- 입력 좌표계가 4326이 아니어도 자동으로 WGS84 로 변환하여 지도에 표시하고,
  폴리곤은 다시 원본 CRS 로 역변환하여 정확히 마스킹합니다.
- 시스템에 다른 PROJ/GDAL(예: MapServer)이 설치되어 `PROJ_LIB` 가 그쪽을
  가리키면 `proj.db` 버전 충돌이 납니다. `app/_bootstrap.py` 가 rasterio
  번들 경로로 자동 교정합니다.
- 현재 업로드/작업 레지스트리는 메모리에 저장됩니다. 프로덕션에서는
  DB + 객체 스토리지로 교체를 권장합니다.
- 프론트 처리 옵션 `feather` 는 블러 경계를 자연스럽게 하기 위한 마스크
  팽창 픽셀입니다.
```
