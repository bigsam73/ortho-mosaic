"""플랫폼 실행 진입점.  python run.py  또는  python run.py --port 8080"""
from app import _bootstrap  # noqa: F401  (PROJ/GDAL 경로 교정)
import argparse
import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run("app.server:app", host=args.host, port=args.port, workers=1)
