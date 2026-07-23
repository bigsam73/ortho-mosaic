// 정사영상 모자이크 플랫폼 - 프론트엔드 로직

// ==== 전체 화면 로딩 오버레이 제어 ====
const overlay = {
  el: null, title: null, sub: null, prog: null, bar: null, pct: null,
  init() {
    this.el = document.getElementById("overlay");
    this.title = document.getElementById("overlayTitle");
    this.sub = document.getElementById("overlaySub");
    this.prog = document.getElementById("overlayProgress");
    this.bar = document.getElementById("overlayBar");
    this.pct = document.getElementById("overlayPct");
  },
  show(title, sub, showBar) {
    this.title.textContent = title || "처리 중...";
    this.sub.textContent = sub || "잠시만 기다려 주세요.";
    this.prog.classList.toggle("show", !!showBar);
    this.bar.style.width = "0%";
    this.pct.textContent = "";
    this.el.classList.add("show");
  },
  setProgress(pct, label) {
    this.prog.classList.add("show");
    this.bar.style.width = pct + "%";
    this.pct.textContent = label || (pct + "%");
  },
  setSub(msg) { this.sub.textContent = msg; },
  hide() { this.el.classList.remove("show"); },
};
overlay.init();

// ==== 지도 타일 로딩 배지 ====
const tileBadge = {
  el: null, text: null, count: 0,
  init() {
    this.el = document.getElementById("tileLoading");
    this.text = document.getElementById("tileLoadingText");
  },
  attach(layer, label) {
    this.count = 0;
    layer.on("loading", () => { this.count++; this.render(label); });
    layer.on("tileloadstart", () => { this.count++; this.render(label); });
    layer.on("tileload", () => { this.count = Math.max(0, this.count - 1); this.render(label); });
    layer.on("tileerror", () => { this.count = Math.max(0, this.count - 1); this.render(label); });
    layer.on("load", () => { this.count = 0; this.render(label); });
  },
  render(label) {
    if (this.count > 0) {
      this.text.textContent = label || "정사영상 타일 로딩 중...";
      this.el.classList.add("show");
    } else {
      this.el.classList.remove("show");
    }
  },
};
tileBadge.init();

const state = {
  id: null,
  bounds: null,        // [minLon, minLat, maxLon, maxLat]
  method: "blur",
  orthoLayer: null,
  drawnItems: null,
  jobTimer: null,
  viewSource: "original",
};

// --- 지도 초기화 ---
const map = L.map("map", { center: [37.5, 127.0], zoom: 5 });
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 24, maxNativeZoom: 19,
  attribution: "&copy; OpenStreetMap",
}).addTo(map);

state.drawnItems = new L.FeatureGroup().addTo(map);

const drawControl = new L.Control.Draw({
  edit: { featureGroup: state.drawnItems },
  draw: {
    polygon: { shapeOptions: { color: "#ff3b3b", weight: 2 } },
    rectangle: { shapeOptions: { color: "#ff3b3b", weight: 2 } },
    polyline: false, circle: false, marker: false, circlemarker: false,
  },
});
map.addControl(drawControl);

map.on(L.Draw.Event.CREATED, (e) => {
  state.drawnItems.addLayer(e.layer);
  updateShapeCount();
});
map.on(L.Draw.Event.DELETED, updateShapeCount);
map.on(L.Draw.Event.EDITED, updateShapeCount);

function updateShapeCount() {
  const n = state.drawnItems.getLayers().length;
  document.getElementById("shapeCount").textContent = `그린 영역: ${n}개`;
}

// --- 업로드 (XHR 로 진행률 표시) ---
document.getElementById("uploadBtn").onclick = () => {
  const f = document.getElementById("fileInput").files[0];
  if (!f) { alert("파일을 선택하세요"); return; }

  const sizeMB = (f.size / 1048576).toFixed(1);
  setStatus("업로드 중...");
  overlay.show("정사영상 업로드 중", `${f.name} (${sizeMB} MB) 전송 중...`, true);

  const fd = new FormData();
  fd.append("file", f);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");

  // 업로드 진행률
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      overlay.setProgress(pct, `${(e.loaded / 1048576).toFixed(1)} / ${sizeMB} MB (${pct}%)`);
      if (pct >= 100) overlay.setSub("서버에서 정사영상 분석 중... (오버뷰/메타데이터 확인)");
    }
  };

  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const info = JSON.parse(xhr.responseText);
      state.id = info.id;
      state.bounds = info.bounds_wgs84;
      showMeta(info);
      overlay.setSub("정사영상 미리보기 타일 로딩 중...");
      loadOrthoLayer("original");
      enableCards();
      setStatus("업로드 완료. 영역을 지정하세요.");
      // 첫 타일 로딩까지 잠깐 보여준 뒤 닫기
      setTimeout(() => overlay.hide(), 600);
    } else {
      overlay.hide();
      let msg = xhr.statusText;
      try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
      setStatus("업로드 실패: " + msg);
      alert("업로드 실패: " + msg);
    }
  };
  xhr.onerror = () => {
    overlay.hide();
    setStatus("업로드 실패: 네트워크 오류");
    alert("업로드 실패: 네트워크 오류");
  };

  xhr.send(fd);
};

function showMeta(info) {
  const mp = (info.width * info.height / 1e6).toFixed(1);
  document.getElementById("meta").innerHTML =
    `크기: ${info.width} × ${info.height} (${mp} MP)<br>` +
    `밴드: ${info.count} · ${info.dtype}<br>` +
    `CRS: ${info.crs || "없음"}<br>` +
    `블록: ${info.block_shape[0]}×${info.block_shape[1]} · 오버뷰 ${info.overviews.length}단계`;
}

function loadOrthoLayer(source) {
  if (state.orthoLayer) map.removeLayer(state.orthoLayer);
  const label = source === "processed"
    ? "처리본 타일 로딩 중..." : "정사영상 타일 로딩 중...";
  state.orthoLayer = L.tileLayer(
    `/api/tiles/${state.id}/{z}/{x}/{y}.png?source=${source}&t=${Date.now()}`,
    { maxZoom: 24, opacity: 1.0, tileSize: 256 }
  );
  tileBadge.attach(state.orthoLayer, label);
  state.orthoLayer.addTo(map);
  const [minLon, minLat, maxLon, maxLat] = state.bounds;
  map.fitBounds([[minLat, minLon], [maxLat, maxLon]]);
}

function enableCards() {
  ["drawCard", "optCard", "runCard"].forEach((id) => {
    const el = document.getElementById(id);
    el.style.opacity = "1";
    el.style.pointerEvents = "auto";
  });
}

// --- 옵션 토글 ---
document.querySelectorAll("#methodToggle button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#methodToggle button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    state.method = b.dataset.m;
    ["blur", "pixelate", "solid"].forEach((m) => {
      document.getElementById("opt-" + m).classList.toggle("hidden", m !== state.method);
    });
  };
});
document.getElementById("blurRadius").oninput = (e) =>
  document.getElementById("brVal").textContent = e.target.value;
document.getElementById("pixelBlock").oninput = (e) =>
  document.getElementById("pbVal").textContent = e.target.value;

document.getElementById("clearBtn").onclick = () => {
  state.drawnItems.clearLayers();
  updateShapeCount();
};

// --- 처리 ---
function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

document.getElementById("processBtn").onclick = async () => {
  const layers = state.drawnItems.getLayers();
  if (layers.length === 0) { alert("모자이크 영역을 먼저 그리세요"); return; }

  const features = layers.map((l) => l.toGeoJSON().geometry);
  const body = {
    id: state.id,
    features,
    method: state.method,
    blur_radius: +document.getElementById("blurRadius").value,
    blur_passes: 3,
    pixel_block: +document.getElementById("pixelBlock").value,
    fill_color: hexToRgb(document.getElementById("fillColor").value),
    feather: +document.getElementById("feather").value,
  };

  setStatus("처리 시작...");
  document.getElementById("progress").style.display = "block";
  document.getElementById("processBtn").disabled = true;
  overlay.show("모자이크 처리 중", "선택 영역을 타일 단위로 처리하고 있습니다...", true);

  const r = await fetch("/api/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    overlay.hide();
    setStatus("처리 실패: " + ((await r.json()).detail || r.statusText));
    document.getElementById("processBtn").disabled = false;
    return;
  }
  const { job_id } = await r.json();
  pollJob(job_id);
};

function pollJob(jobId) {
  if (state.jobTimer) clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    const r = await fetch("/api/job/" + jobId);
    const j = await r.json();
    if (j.total > 0) {
      const pct = Math.round((j.done / j.total) * 100);
      document.getElementById("progressBar").style.width = pct + "%";
      setStatus(`처리 중... ${j.done}/${j.total} 블록 (${pct}%)`);
      overlay.setProgress(pct, `${j.done} / ${j.total} 블록 처리 (${pct}%)`);
    }
    if (j.status === "done") {
      clearInterval(state.jobTimer);
      const res = j.result || {};
      setStatus(`완료! 수정된 블록: ${res.blocks_modified}/${res.blocks_total}`);
      // 저장(정사영상 + 오버뷰 생성) 및 처리본 타일 로딩 안내
      overlay.setProgress(100, "정사영상 저장 및 미리보기 생성 중...");
      overlay.setSub("결과 정사영상(GeoTIFF/오버뷰)을 반영하고 있습니다...");
      document.getElementById("processBtn").disabled = false;
      document.getElementById("downloadBtn").classList.remove("hidden");
      document.getElementById("viewToggle").style.display = "flex";
      loadOrthoLayer("processed");
      setActiveView("processed");
      setTimeout(() => overlay.hide(), 700);
    } else if (j.status === "error") {
      clearInterval(state.jobTimer);
      overlay.hide();
      setStatus("오류: " + j.error);
      document.getElementById("processBtn").disabled = false;
    }
  }, 400);
}

document.getElementById("downloadBtn").onclick = async () => {
  overlay.show("결과 정사영상 저장 중", "서버에서 파일을 내려받고 있습니다...", true);
  try {
    const resp = await fetch("/api/download/" + state.id);
    if (!resp.ok) throw new Error("다운로드 실패 (" + resp.status + ")");

    const total = +resp.headers.get("Content-Length") || 0;
    const reader = resp.body.getReader();
    const chunks = [];
    let received = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.length;
      if (total > 0) {
        const pct = Math.round((received / total) * 100);
        overlay.setProgress(pct,
          `${(received / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MB (${pct}%)`);
      } else {
        overlay.setSub(`${(received / 1048576).toFixed(1)} MB 수신 중...`);
      }
    }

    overlay.setSub("파일 저장 준비 중...");
    const blob = new Blob(chunks, { type: "image/tiff" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = state.id + "_mosaic.tif";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    overlay.hide();
    setStatus("정사영상 저장 완료.");
  } catch (err) {
    overlay.hide();
    alert(err.message);
    setStatus(err.message);
  }
};

// --- 원본/처리본 보기 토글 ---
document.querySelectorAll("#viewToggle button").forEach((b) => {
  b.onclick = () => { loadOrthoLayer(b.dataset.src); setActiveView(b.dataset.src); };
});
function setActiveView(src) {
  document.querySelectorAll("#viewToggle button").forEach((x) =>
    x.classList.toggle("active", x.dataset.src === src));
}

function setStatus(msg) { document.getElementById("status").textContent = msg; }
