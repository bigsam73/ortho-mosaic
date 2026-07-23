"""
고속 모자이크 픽셀 커널.

핵심 설계:
- Numba @njit(parallel=True, fastmath=True) 로 LLVM 네이티브 코드 컴파일 + 멀티코어 SIMD.
  ("인라인 어셈블러처럼" 빠르게 = LLVM 백엔드가 AVX/SSE 벡터화 자동 적용)
- 모든 커널은 하나의 타일(window) 배열에서 in-place 로 동작 -> 메모리 복사 최소화.
- band-interleaved (H, W, C) 레이아웃 기준. C(밴드)는 안쪽 루프에 두어 캐시 지역성 확보.
- mask: uint8 (H, W), 값 != 0 인 픽셀만 처리 대상.

배열 dtype 은 uint8 (8bit RGB/RGBA 정사영상) 기준으로 최적화.
"""

import numpy as np
from numba import njit, prange


# ---------------------------------------------------------------------------
# 1) 단색 채우기 (solid fill) - 가장 빠름, 단순 대입
# ---------------------------------------------------------------------------
@njit(parallel=True, fastmath=True, nogil=True)
def fill_solid(img, mask, color):
    """
    img:   (H, W, C) uint8, in-place 수정
    mask:  (H, W)    uint8, !=0 이면 채움
    color: (C,)      uint8
    """
    h, w, c = img.shape
    for y in prange(h):
        for x in range(w):
            if mask[y, x] != 0:
                for k in range(c):
                    img[y, x, k] = color[k]


# ---------------------------------------------------------------------------
# 2) 픽셀화 (mosaic / pixelate) - 블록 평균으로 뭉개기
# ---------------------------------------------------------------------------
@njit(parallel=True, fastmath=True, nogil=True)
def pixelate(img, mask, block):
    """
    img:   (H, W, C) uint8, in-place
    mask:  (H, W)    uint8
    block: 픽셀 블록 크기 (예: 16 -> 16x16 블록 평균)

    블록 단위로 순회. 블록 안에 mask!=0 픽셀이 하나라도 있으면
    그 블록 전체 평균색으로 (마스크된 픽셀만) 대체.
    """
    h, w, c = img.shape
    nby = (h + block - 1) // block
    nbx = (w + block - 1) // block

    for by in prange(nby):
        y0 = by * block
        y1 = y0 + block
        if y1 > h:
            y1 = h
        for bx in range(nbx):
            x0 = bx * block
            x1 = x0 + block
            if x1 > w:
                x1 = w

            acc = np.zeros(c, dtype=np.float64)
            n = 0
            has_mask = False
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if mask[y, x] != 0:
                        has_mask = True
                    for k in range(c):
                        acc[k] += img[y, x, k]
                    n += 1

            if not has_mask or n == 0:
                continue

            avg = np.empty(c, dtype=np.uint8)
            for k in range(c):
                avg[k] = np.uint8(acc[k] / n + 0.5)

            for y in range(y0, y1):
                for x in range(x0, x1):
                    if mask[y, x] != 0:
                        for k in range(c):
                            img[y, x, k] = avg[k]


# ---------------------------------------------------------------------------
# 3) 박스 블러 (box blur) - 분리형 슬라이딩 윈도우로 O(N) 처리
# ---------------------------------------------------------------------------
@njit(parallel=True, fastmath=True, nogil=True)
def _box_blur_pass(src, dst, radius, horizontal):
    """
    한 방향(가로 또는 세로) 슬라이딩 윈도우 평균.
    src, dst: (H, W, C) float32
    """
    h, w, c = src.shape
    win = 2 * radius + 1

    if horizontal:
        for y in prange(h):
            for k in range(c):
                s = 0.0
                for x in range(-radius, radius + 1):
                    xi = x
                    if xi < 0:
                        xi = 0
                    elif xi >= w:
                        xi = w - 1
                    s += src[y, xi, k]
                for x in range(w):
                    dst[y, x, k] = s / win
                    x_out = x - radius
                    if x_out < 0:
                        x_out = 0
                    x_in = x + radius + 1
                    if x_in >= w:
                        x_in = w - 1
                    s += src[y, x_in, k] - src[y, x_out, k]
    else:
        for x in prange(w):
            for k in range(c):
                s = 0.0
                for y in range(-radius, radius + 1):
                    yi = y
                    if yi < 0:
                        yi = 0
                    elif yi >= h:
                        yi = h - 1
                    s += src[yi, x, k]
                for y in range(h):
                    dst[y, x, k] = s / win
                    y_out = y - radius
                    if y_out < 0:
                        y_out = 0
                    y_in = y + radius + 1
                    if y_in >= h:
                        y_in = h - 1
                    s += src[y_in, x, k] - src[y_out, x, k]


@njit(parallel=True, fastmath=True, nogil=True)
def _composite_masked(img, blurred, mask):
    """블러 결과를 마스크 픽셀에만 되쓰기 (in-place)."""
    h, w, c = img.shape
    for y in prange(h):
        for x in range(w):
            if mask[y, x] != 0:
                for k in range(c):
                    v = blurred[y, x, k]
                    if v < 0.0:
                        v = 0.0
                    elif v > 255.0:
                        v = 255.0
                    img[y, x, k] = np.uint8(v + 0.5)


def box_blur(img, mask, radius, passes=3):
    """
    img:  (H, W, C) uint8, in-place
    mask: (H, W)    uint8
    radius: 블러 반경
    passes: 박스블러 반복 횟수 (3회 반복 = 가우시안 근사)

    2-pass separable box blur 를 passes 회 반복. 전체는 float32 로 계산 후
    마스크 픽셀만 되쓴다.
    """
    if radius < 1:
        return
    src = img.astype(np.float32)
    tmp = np.empty_like(src)
    for _ in range(passes):
        _box_blur_pass(src, tmp, radius, True)    # 가로
        _box_blur_pass(tmp, src, radius, False)   # 세로 -> src 에 결과
    _composite_masked(img, src, mask)


# ---------------------------------------------------------------------------
# 커널 워밍업 (첫 호출 JIT 컴파일 지연을 서버 기동 시 미리 처리)
# ---------------------------------------------------------------------------
def warmup():
    dummy = np.zeros((4, 4, 3), dtype=np.uint8)
    m = np.ones((4, 4), dtype=np.uint8)
    fill_solid(dummy.copy(), m, np.array([0, 0, 0], dtype=np.uint8))
    pixelate(dummy.copy(), m, 2)
    box_blur(dummy.copy(), m, 1, passes=1)
