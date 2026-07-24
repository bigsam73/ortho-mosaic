"""HTTP API 엔드투엔드 테스트 (urllib 사용, 외부 의존성 없음)."""
import json
import time
import urllib.request
import uuid

# 프록시 우회 (시스템 프록시/no_proxy 처리 이슈 회피)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_opener)

BASE = "http://127.0.0.1:8000"


def post_multipart(url, filepath, field="file"):
    boundary = uuid.uuid4().hex
    with open(filepath, "rb") as f:
        data = f.read()
    fname = filepath.split("\\")[-1]
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="{field}"; filename="{fname}"\r\n'.encode()
    body += b"Content-Type: image/tiff\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def post_json(url, obj):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


# 1) upload
up = post_multipart(f"{BASE}/api/upload", r"C:\opencode\ortho-mosaic\data\uploads\sample.tif")
print("UPLOAD:", up["id"], f'{up["width"]}x{up["height"]}', "crs=", up["crs"],
      "bounds=", up["bounds_wgs84"])
oid = up["id"]

# 2) tile (원본) - bounds 중앙 근처 z16 타일 하나
import math
lon = (up["bounds_wgs84"][0] + up["bounds_wgs84"][2]) / 2
lat = (up["bounds_wgs84"][1] + up["bounds_wgs84"][3]) / 2
z = 16
n = 2 ** z
xt = int((lon + 180) / 360 * n)
yt = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
st, ct, data = get(f"{BASE}/api/tiles/{oid}/{z}/{xt}/{yt}.png?source=original")
print("TILE:", st, ct, len(data), "bytes  (z/x/y=", z, xt, yt, ")")

# 3) process (blur)
feat = {"type": "Polygon", "coordinates": [[
    [126.979, 37.5625], [126.9825, 37.5625],
    [126.9825, 37.565], [126.979, 37.565], [126.979, 37.5625]]]}
pr = post_json(f"{BASE}/api/process", {
    "id": oid, "features": [feat], "method": "blur",
    "blur_radius": 12, "blur_passes": 3, "feather": 4})
job = pr["job_id"]
print("PROCESS job:", job)

# 4) poll
for _ in range(40):
    time.sleep(0.4)
    _, _, jb = get(f"{BASE}/api/job/{job}")
    j = json.loads(jb)
    if j["status"] != "running":
        break
print("JOB:", j["status"], f'{j["done"]}/{j["total"]}',
      "modified=", j.get("result", {}).get("blocks_modified"))
assert j["status"] == "done"

# 5) processed tile
st2, _, data2 = get(f"{BASE}/api/tiles/{oid}/{z}/{xt}/{yt}.png?source=processed")
print("PROCESSED TILE:", st2, len(data2), "bytes")

# 6) download
st3, ct3, dl = get(f"{BASE}/api/download/{oid}")
print("DOWNLOAD:", st3, ct3, len(dl), "bytes")
assert len(dl) > 1000
print("\nAPI 전체 흐름 통과")
