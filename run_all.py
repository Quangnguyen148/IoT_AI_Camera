"""
run_all.py – Khởi chạy đồng thời Gateway Backend (T08 + T07 + AI) và Streamlit UI (T10)
Sử dụng:
    python run_all.py
    python run_all.py --mock
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    extra_args = sys.argv[1:]
    gateway_cmd = [sys.executable, str(BASE_DIR / "gateway_main.py"), *extra_args]
    streamlit_cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(BASE_DIR / "streamlit_app.py"),
        "--server.port=8501",
    ]

    print("[RUN_ALL] Đang khởi động IoT Gateway Backend (T08 + T07)...")
    gw_proc = subprocess.Popen(gateway_cmd, cwd=str(BASE_DIR))
    time.sleep(2.0)

    print("[RUN_ALL] Đang khởi động Streamlit Dashboard (T10) tại http://localhost:8501 ...")
    st_proc = subprocess.Popen(streamlit_cmd, cwd=str(BASE_DIR))

    try:
        gw_proc.wait()
    except KeyboardInterrupt:
        print("\n[RUN_ALL] Đang dừng toàn bộ dịch vụ...")
    finally:
        st_proc.terminate()
        gw_proc.terminate()


if __name__ == "__main__":
    main()