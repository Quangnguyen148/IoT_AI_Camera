"""
web_server.py – Thành phần T07: Local Webserver trên thiết bị (FastAPI)
======================================================================
Chức năng:
  1. GET /video_feed : Phát luồng video MJPEG trực tiếp từ Webcam (qua
     VisionAgent.get_latest_frame()) để thiết bị trong mạng LAN xem trực tiếp.
  2. GET / & POST /config : Trang HTML giám sát nhanh & form cấu hình động
     (CoreIOT Host, Port, Access Token, Gemini API Key, ngưỡng Quarantine).
  3. /api/* : Internal REST API phục vụ giao diện Streamlit (T10) đồng bộ
     trạng thái AI, bật/tắt AI và truy xuất lịch sử log.
"""

from __future__ import annotations

import html
import socket
import time
from datetime import datetime
from typing import Callable

import cv2
import numpy as np
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from config import ConfigManager
from state_store import StateStore


class AIToggleRequest(BaseModel):
    status: bool


def _get_lan_ip() -> str:
    """Lấy địa chỉ IP LAN hiện tại của máy tính để hiển thị hướng dẫn truy cập."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _make_placeholder_jpeg(title: str, subtitle: str) -> bytes:
    """Tạo khung hình JPEG thông báo khi camera đang khởi động hoặc chạy Mock mode."""
    canvas = np.zeros((480, 640, 3), dtype=np.uint8)
    canvas[:] = (24, 28, 36)
    cv2.rectangle(canvas, (24, 24), (616, 456), (56, 189, 248), 2)
    cv2.putText(canvas, title, (48, 220), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)
    cv2.putText(canvas, subtitle, (48, 265), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (148, 163, 184), 1)
    ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return buf.tobytes() if ok else b""


def create_web_app(
    config_manager: ConfigManager,
    state_store: StateStore,
    get_frame_fn: Callable[[], np.ndarray | None],
    set_ai_status_fn: Callable[[bool], None],
    get_ai_status_fn: Callable[[], bool],
) -> FastAPI:
    """Khởi tạo ứng dụng FastAPI gắn với các callback của Gateway."""
    app = FastAPI(title="IoT Edge Gateway Local Webserver (T07)", version="1.0.0")

    def _mjpeg_generator():
        while True:
            frame = get_frame_fn()
            snapshot = state_store.get_snapshot()
            ai_active = snapshot["ai_active"]
            people = snapshot["latest_telemetry"].get("people_count", 0)

            if frame is None:
                jpg_bytes = _make_placeholder_jpeg(
                    "Camera_AI_01 - Waiting for Video Frame",
                    f"AI Status: {'ACTIVE' if ai_active else 'PAUSED'} | Time: {datetime.now().strftime('%H:%M:%S')}",
                )
            else:
                # Vẽ HUD trạng thái lên bản sao frame để không ảnh hưởng ảnh gốc
                overlay = frame.copy()
                badge_color = (34, 197, 94) if ai_active else (59, 130, 246)
                status_label = f"AI: ACTIVE | People: {people}" if ai_active else "AI: PAUSED"
                cv2.rectangle(overlay, (10, 10), (350, 46), (15, 23, 42), -1)
                cv2.putText(overlay, status_label, (20, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.65, badge_color, 2)
                cv2.putText(
                    overlay,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    (10, overlay.shape[0] - 14),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (226, 232, 240),
                    1,
                )
                ok, buf = cv2.imencode(".jpg", overlay, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                jpg_bytes = buf.tobytes() if ok else b""

            if jpg_bytes:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n"
                )
            time.sleep(0.05)  # ~20 FPS cho LAN stream mượt và nhẹ CPU

    @app.get("/video_feed")
    def video_feed():
        return StreamingResponse(
            _mjpeg_generator(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/api/status")
    def api_status():
        cfg = config_manager.get()
        snap = state_store.get_snapshot()
        snap["ai_active"] = bool(get_ai_status_fn())
        snap["lan_url"] = f"http://{_get_lan_ip()}:{cfg.webserver_port}"
        snap["config"] = {
            "coreiot_host": cfg.coreiot_host,
            "coreiot_port": cfg.coreiot_port,
            "gateway_name": cfg.gateway_name,
            "device_name": cfg.device_name,
            "conf_threshold": cfg.conf_threshold,
            "quarantine_frames": cfg.quarantine_frames,
            "causal_window_sec": cfg.causal_window_sec,
            "has_gateway_token": bool(cfg.gateway_access_token and not cfg.gateway_access_token.startswith("YOUR_")),
            "has_gemini_key": bool(cfg.gemini_api_key and not cfg.gemini_api_key.startswith("YOUR_")),
        }
        return JSONResponse(snap)

    @app.post("/api/ai/toggle")
    def api_ai_toggle(payload: AIToggleRequest):
        set_ai_status_fn(payload.status)
        actual = bool(get_ai_status_fn())
        return JSONResponse({"ok": True, "ai_active": actual})

    @app.get("/api/logs")
    def api_logs(limit: int = 100):
        return JSONResponse(
            {
                "telemetry": state_store.get_recent_telemetry(limit=limit),
                "rpcs": state_store.get_recent_rpcs(limit=50),
            }
        )

    @app.post("/api/logs/clear")
    def api_clear_logs():
        state_store.clear_logs()
        return JSONResponse({"ok": True})

    @app.post("/config")
    def save_config(
        coreiot_host: str = Form(...),
        coreiot_port: int = Form(...),
        gateway_access_token: str = Form(""),
        gemini_api_key: str = Form(""),
        conf_threshold: float = Form(0.6),
        quarantine_frames: int = Form(15),
        causal_window_sec: float = Form(5.0),
    ):
        updates = {
            "coreiot_host": coreiot_host.strip(),
            "coreiot_port": int(coreiot_port),
            "conf_threshold": float(conf_threshold),
            "quarantine_frames": int(quarantine_frames),
            "causal_window_sec": float(causal_window_sec),
        }
        if gateway_access_token.strip():
            updates["gateway_access_token"] = gateway_access_token.strip()
        if gemini_api_key.strip():
            updates["gemini_api_key"] = gemini_api_key.strip()

        config_manager.update(updates)
        return RedirectResponse(url="/?saved=1", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(saved: int = 0):
        cfg = config_manager.get()
        snap = state_store.get_snapshot()
        lan_url = f"http://{_get_lan_ip()}:{cfg.webserver_port}"
        banner = (
            '<div class="alert-ok">Đã lưu cấu hình và đồng bộ nóng tới Gateway thành công!</div>'
            if saved
            else ""
        )

        page = f"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>IoT Edge Gateway - Local Webserver (T07)</title>
  <style>
    :root {{
      --bg: #0b1120; --card: #111827; --border: #1f2937;
      --text: #f1f5f9; --muted: #94a3b8; --accent: #38bdf8;
      --ok: #22c55e; --warn: #ef4444;
    }}
    * {{ box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }}
    body {{ margin: 0; background: var(--bg); color: var(--text); padding: 20px; }}
    .container {{ max-width: 1180px; margin: 0 auto; }}
    header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 14px; }}
    h1 {{ margin: 0; font-size: 1.35rem; }}
    .sub {{ color: var(--muted); font-size: 0.88rem; margin-top: 4px; }}
    .grid {{ display: grid; grid-template-columns: 1.25fr 1fr; gap: 20px; }}
    @media (max-width: 900px) {{ .grid {{ grid-template-columns: 1fr; }} }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px; }}
    .card h2 {{ margin: 0 0 14px 0; font-size: 1.05rem; color: var(--accent); }}
    .video-box {{ width: 100%; border-radius: 8px; overflow: hidden; border: 1px solid var(--border); background: #000; }}
    .video-box img {{ width: 100%; display: block; }}
    .status-row {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; align-items: center; }}
    .pill {{ padding: 5px 12px; border-radius: 999px; font-size: 0.82rem; font-weight: 600; background: #1e293b; }}
    .pill.ok {{ color: var(--ok); border: 1px solid rgba(34,197,94,0.35); }}
    .pill.bad {{ color: var(--warn); border: 1px solid rgba(239,68,68,0.35); }}
    .btn {{ cursor: pointer; border: none; border-radius: 8px; padding: 9px 15px; font-weight: 600; font-size: 0.88rem; }}
    .btn-primary {{ background: var(--accent); color: #082f49; }}
    .btn-toggle {{ background: #1e293b; color: var(--text); border: 1px solid #334155; }}
    .form-group {{ margin-bottom: 12px; }}
    label {{ display: block; font-size: 0.82rem; color: var(--muted); margin-bottom: 5px; }}
    input {{ width: 100%; padding: 9px 11px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: var(--text); font-size: 0.9rem; }}
    .row-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
    .alert-ok {{ background: rgba(34,197,94,0.15); border: 1px solid var(--ok); color: #86efac; padding: 10px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 0.9rem; }}
    .alert-box {{ margin-top: 12px; padding: 10px 12px; border-radius: 8px; background: #0f172a; border-left: 3px solid var(--accent); font-size: 0.88rem; color: #e2e8f0; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>IoT Edge Gateway – Local Webserver (T07)</h1>
        <div class="sub">Gateway: <b>{html.escape(cfg.gateway_name)}</b> | Thiết bị con: <b>{html.escape(cfg.device_name)}</b> | Truy cập LAN: <b>{html.escape(lan_url)}</b></div>
      </div>
      <div class="status-row" style="margin-top:0;">
        <span id="mqtt-pill" class="pill {'ok' if snap['mqtt_connected'] else 'bad'}">MQTT: {html.escape(snap['mqtt_status_msg'])}</span>
        <span id="ai-pill" class="pill {'ok' if snap['ai_active'] else 'bad'}">AI: {'ACTIVE' if snap['ai_active'] else 'PAUSED'}</span>
        <button class="btn btn-toggle" onclick="toggleAI()">Bật / Tắt AI</button>
      </div>
    </header>
    {banner}
    <div class="grid">
      <div class="card">
        <h2>Luồng Camera Trực Tiếp (LAN MJPEG Stream)</h2>
        <div class="video-box">
          <img src="/video_feed" alt="Live Camera Stream" />
        </div>
        <div class="status-row">
          <span class="pill">Số người xác nhận: <b id="people-count">{snap['latest_telemetry'].get('people_count', 0)}</b></span>
          <span class="pill">Cập nhật cuối: <b id="last-ts">{html.escape(snap['last_publish_iso'])}</b></span>
        </div>
        <div class="alert-box">
          <b>Nhận xét LLM mới nhất:</b> <span id="llm-alert">{html.escape(snap['latest_telemetry'].get('llm_alert') or 'Chưa có cảnh báo')}</span>
        </div>
      </div>

      <div class="card">
        <h2>Cấu Hình Gateway & Ngưỡng Cảnh Báo</h2>
        <form method="post" action="/config">
          <div class="row-2">
            <div class="form-group">
              <label>CoreIOT Broker Host</label>
              <input type="text" name="coreiot_host" value="{html.escape(cfg.coreiot_host)}" required />
            </div>
            <div class="form-group">
              <label>CoreIOT MQTT Port</label>
              <input type="number" name="coreiot_port" value="{cfg.coreiot_port}" required />
            </div>
          </div>
          <div class="form-group">
            <label>Gateway Access Token (Để trống nếu giữ nguyên token cũ)</label>
            <input type="password" name="gateway_access_token" placeholder="{'Đã cấu hình (nhập mới để thay đổi)' if cfg.gateway_access_token else 'Nhập Access Token từ CoreIOT...'}" />
          </div>
          <div class="form-group">
            <label>Gemini API Key (Để trống nếu giữ nguyên key cũ)</label>
            <input type="password" name="gemini_api_key" placeholder="{'Đã cấu hình (nhập mới để thay đổi)' if cfg.gemini_api_key else 'Nhập GEMINI_API_KEY...'}" />
          </div>
          <div class="row-2">
            <div class="form-group">
              <label>Ngưỡng Confidence Quarantine (0.05 – 0.99)</label>
              <input type="number" step="0.05" min="0.05" max="0.99" name="conf_threshold" value="{cfg.conf_threshold}" required />
            </div>
            <div class="form-group">
              <label>Số Frame Liên Tiếp (Quarantine Frames)</label>
              <input type="number" min="1" max="120" name="quarantine_frames" value="{cfg.quarantine_frames}" required />
            </div>
          </div>
          <div class="form-group">
            <label>Chu kỳ Causal Window (giây)</label>
            <input type="number" step="0.5" min="1" max="60" name="causal_window_sec" value="{cfg.causal_window_sec}" required />
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;">Lưu Cấu Hình & Áp Dụng Ngay</button>
        </form>
      </div>
    </div>
  </div>

  <script>
    let currentAI = {'true' if snap['ai_active'] else 'false'};
    async function toggleAI() {{
      const res = await fetch('/api/ai/toggle', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ status: !currentAI }})
      }});
      const data = await res.json();
      currentAI = data.ai_active;
      refreshStatus();
    }}
    async function refreshStatus() {{
      try {{
        const res = await fetch('/api/status');
        const s = await res.json();
        currentAI = s.ai_active;
        const aiPill = document.getElementById('ai-pill');
        aiPill.textContent = 'AI: ' + (s.ai_active ? 'ACTIVE' : 'PAUSED');
        aiPill.className = 'pill ' + (s.ai_active ? 'ok' : 'bad');

        const mqttPill = document.getElementById('mqtt-pill');
        mqttPill.textContent = 'MQTT: ' + s.mqtt_status_msg;
        mqttPill.className = 'pill ' + (s.mqtt_connected ? 'ok' : 'bad');

        document.getElementById('people-count').textContent = s.latest_telemetry.people_count ?? 0;
        document.getElementById('last-ts').textContent = s.last_publish_iso;
        document.getElementById('llm-alert').textContent = s.latest_telemetry.llm_alert || 'Không phát hiện người / An toàn';
      }} catch (e) {{}}
    }}
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>"""
        return HTMLResponse(content=page)

    return app