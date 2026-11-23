"""
config.py – Quản lý cấu hình tập trung cho IoT Gateway (T08 & T07)
Hỗ trợ đọc từ biến môi trường (.env), ghi đè từ file gateway_config.json
(khi người dùng lưu cấu hình qua trang HTML T07) và kích hoạt hot-reload.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    BASE_DIR = Path(__file__).resolve().parent
    ENV_PATH = BASE_DIR / ".env"
    load_dotenv(dotenv_path=ENV_PATH, override=False)
except ImportError:
    BASE_DIR = Path(__file__).resolve().parent
    ENV_PATH = BASE_DIR / ".env"

CONFIG_JSON_PATH = BASE_DIR / "gateway_config.json"


class GatewayConfig(BaseModel):
    """Schema cấu hình hệ thống IoT Gateway."""

    coreiot_host: str = Field(default="app.coreiot.io", description="Địa chỉ CoreIOT MQTT Broker")
    coreiot_port: int = Field(default=1883, ge=1, le=65535, description="Cổng MQTT Broker")
    gateway_name: str = Field(default="BTL_PC_GATEWAY", description="Tên Gateway trên CoreIOT")
    device_name: str = Field(default="Camera_AI_01", description="Tên thiết bị con mặc định")
    gateway_access_token: str = Field(default="", description="Access Token của Gateway trên CoreIOT")

    gemini_api_key: str = Field(default="", description="Google Gemini API Key")
    gemini_model: str = Field(default="gemini-2.0-flash", description="Tên model Gemini")
    yolo_model_path: str = Field(default="yolov8n.pt", description="Đường dẫn weights YOLO")
    camera_index: int = Field(default=0, ge=0, description="Chỉ số Webcam")

    conf_threshold: float = Field(default=0.6, ge=0.05, le=0.99, description="Ngưỡng confidence Quarantine")
    quarantine_frames: int = Field(default=15, ge=1, le=120, description="Số frame liên tục xác nhận")
    causal_window_sec: float = Field(default=5.0, ge=1.0, le=60.0, description="Chu kỳ Causal Window (giây)")

    webserver_host: str = Field(default="0.0.0.0", description="Host bind của Webserver T07")
    webserver_port: int = Field(default=8000, ge=1024, le=65535, description="Port của Webserver T07")


class ConfigManager:
    """Quản lý đọc/ghi cấu hình thread-safe và thông báo cho các module khi thay đổi."""

    def __init__(self, json_path: Path = CONFIG_JSON_PATH) -> None:
        self._lock = threading.Lock()
        self._json_path = json_path
        self._listeners: list[Callable[[GatewayConfig], None]] = []
        self._config = self._load_initial()

    def _load_initial(self) -> GatewayConfig:
        raw: dict = {
            "coreiot_host": os.getenv("COREIOT_HOST", "app.coreiot.io"),
            "coreiot_port": int(os.getenv("COREIOT_PORT", "1883")),
            "gateway_name": os.getenv("GATEWAY_NAME", "BTL_PC_GATEWAY"),
            "device_name": os.getenv("DEVICE_NAME", "Camera_AI_01"),
            "gateway_access_token": os.getenv("GATEWAY_ACCESS_TOKEN", ""),
            "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
            "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
            "yolo_model_path": os.getenv("YOLO_MODEL_PATH", "yolov8n.pt"),
            "camera_index": int(os.getenv("CAMERA_INDEX", "0")),
            "conf_threshold": float(os.getenv("CONF_THRESHOLD", "0.6")),
            "quarantine_frames": int(os.getenv("QUARANTINE_FRAMES", "15")),
            "causal_window_sec": float(os.getenv("CAUSAL_WINDOW_SEC", "5.0")),
            "webserver_host": os.getenv("WEBSERVER_HOST", "0.0.0.0"),
            "webserver_port": int(os.getenv("WEBSERVER_PORT", "8000")),
        }

        if self._json_path.exists():
            try:
                saved = json.loads(self._json_path.read_text(encoding="utf-8"))
                raw.update({k: v for k, v in saved.items() if v is not None})
            except Exception:
                pass

        return GatewayConfig(**raw)

    def get(self) -> GatewayConfig:
        with self._lock:
            return self._config.model_copy()

    def register_listener(self, callback: Callable[[GatewayConfig], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def update(self, updates: dict) -> GatewayConfig:
        with self._lock:
            merged = self._config.model_dump()
            for key, value in updates.items():
                if key in merged and value is not None:
                    merged[key] = value
            updated_cfg = GatewayConfig(**merged)
            self._config = updated_cfg
            self._json_path.write_text(
                json.dumps(updated_cfg.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            listeners = list(self._listeners)

        for callback in listeners:
            callback(updated_cfg)

        return updated_cfg