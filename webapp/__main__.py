"""启动 Web 端证据链页面（FR-G01）。

    .venv/Scripts/python.exe -m webapp            # 推荐：从项目根以模块方式启动
    .venv/Scripts/python.exe webapp/__main__.py   # 直接运行本文件也可以

仅监听 127.0.0.1：本机演示用途，不对局域网暴露（安全基线：宁少勿多）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 直接运行本文件时，sys.path[0] 是 webapp/ 而非项目根，src/webapp 包都找不到。
# 与 main.py 同款自救：把项目根塞回模块搜索路径。
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import settings  # noqa: E402,F401


def main() -> int:
    ap = argparse.ArgumentParser(description="税海拾珠 · Web 端证据链页面")
    ap.add_argument("--host", default=os.getenv("TAXPEARLS_HOST", "127.0.0.1"), help="监听地址")
    ap.add_argument("--port", type=int, default=int(os.getenv("TAXPEARLS_PORT", "8000")), help="监听端口")
    args = ap.parse_args()

    import uvicorn

    print(f"  税海拾珠 Web 端启动中 → http://{args.host}:{args.port}")
    uvicorn.run("webapp.app:app", host=args.host, port=args.port, log_level="warning",
                proxy_headers=True,
                forwarded_allow_ips=os.getenv("TAXPEARLS_TRUSTED_PROXY_IPS", "127.0.0.1"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
