"""
gateway_main.py – Tiến trình lõi của IoT Edge Gateway (Thành viên 2)
===================================================================
Kết nối và điều phối đồng bộ 3 thành phần:
  1. Edge AI Module (vision_agent.py – Thành viên 1)
  2. IoT Gateway MQTT Bridge (mqtt_gateway.py – T08)
  3. Local Webserver & API (web_server.py – T07)

Chạy thực tế:
    python gateway_main.py

Chạy giả lập không cần Webcam (để test MQTT/CoreIOT/UI):
    python gateway_main.py --mock
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import sys
import threading
import time

import uvicorn

from config import ConfigManager, GatewayConfig
from mqtt_gateway import CoreIOTGatewayClient
from state_store import StateStore
from vision_agent import VisionAgent
from web_server import create_web_app

# Đảm bảo log tiếng Việt không lỗi encoding trên Windows Console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("GatewayMain")


class GatewayOrchestrator:
    """Điều phối vòng đời giữa VisionAgent, MQTT Gateway (T08) và Webserver (T07)."""

    def __init__(self, mock_mode: bool = False) -> None:
        self._mock_mode = mock_mode
        self._stop_event = threading.Event()

        self.config_manager = ConfigManager()
        self.state_store = StateStore()
        cfg = self.config_manager.get()

        self._agent: VisionAgent | None = None
        if not self._mock_mode:
            self._agent = VisionAgent(
                gemini_api_key=cfg.gemini_api_key or None,
                gemini_model=cfg.gemini_model,
                yolo_model_path=cfg.yolo_model_path,
                camera_index=cfg.camera_index,
                conf_threshold=cfg.conf_threshold,
                quarantine_frames=cfg.quarantine_frames,
                causal_window_sec=cfg.causal_window_sec,
            )

        self.mqtt_gateway = CoreIOTGatewayClient(
            config=cfg,
            state_store=self.state_store,
            on_set_ai_status=lambda s, **kw: self.set_ai_status(
                s,
                source=kw.get("source", "CoreIOT_RPC"),
                sync_cloud=kw.get("sync_cloud", False),
            ),
            on_get_ai_status=self.get_ai_status,
        )

        self.config_manager.register_listener(self._on_config_updated)

        self.web_app = create_web_app(
            config_manager=self.config_manager,
            state_store=self.state_store,
            get_frame_fn=self.get_latest_frame,
            set_ai_status_fn=lambda s: self.set_ai_status(s, source="WebAPI", sync_cloud=True),
            get_ai_status_fn=self.get_ai_status,
        )

    def set_ai_status(self, status: bool, source: str = "Local", sync_cloud: bool = True) -> None:
        status_bool = bool(status)
        current_status = self.get_ai_status()
        if status_bool == current_status:
            logger.debug("[Gateway] Trạng thái AI không đổi (%s) từ %s – bỏ qua.", status_bool, source)
            return

        if self._agent is not None:
            self._agent.set_ai_status(status_bool)
        self.state_store.set_ai_active(status_bool)
        logger.info(
            "[Gateway] Trạng thái AI đã chuyển sang: %s (Nguồn: %s | Agent=%s, StateStore=%s)",
            "ACTIVE (BẬT)" if status_bool else "PAUSED (TẮT)",
            source,
            self._agent is not None,
            True,
        )
        if sync_cloud and hasattr(self, "mqtt_gateway") and self.mqtt_gateway is not None:
            self.mqtt_gateway.publish_ai_status(status_bool)

    def get_ai_status(self) -> bool:
        if self._agent is not None:
            return bool(self._agent.get_ai_status())
        return self.state_store.get_ai_active()

    def get_latest_frame(self):
        if self._agent is not None:
            return self._agent.get_latest_frame()
        return None

    def _on_config_updated(self, new_cfg: GatewayConfig) -> None:
        """Hot-reload thông số AI và kết nối MQTT an toàn khi lưu cấu hình từ T07."""
        logger.info("[Gateway] Nhận tín hiệu cấu hình mới từ ConfigManager, bắt đầu hot-reload...")
        if self._agent is not None:
            self._agent.update_config(
                conf_threshold=new_cfg.conf_threshold,
                quarantine_frames=new_cfg.quarantine_frames,
                causal_window_sec=new_cfg.causal_window_sec,
                gemini_api_key=new_cfg.gemini_api_key or None,
                gemini_model=new_cfg.gemini_model,
            )
        self.mqtt_gateway.reload(new_cfg)
        logger.info("[Gateway] Quá trình hot-reload cấu hình hoàn tất thành công.")

    def _ai_worker_loop(self) -> None:
        """Luồng nhận kết quả từ VisionAgent (mỗi Causal Window 5s) và đẩy qua MQTT."""
        if self._mock_mode:
            logger.info("Khởi chạy AI Worker ở chế độ MOCK (giả lập chu kỳ 5 giây)...")
            while not self._stop_event.is_set():
                window_sec = self.config_manager.get().causal_window_sec
                if self._stop_event.wait(timeout=window_sec):
                    break

                if not self.get_ai_status():
                    payload = {"people_count": 0, "anomaly_detected": False, "llm_alert": "", "ai_active": False}
                else:
                    count = random.choice([0, 0, 1, 2])
                    payload = {
                        "people_count": count,
                        "anomaly_detected": count > 0,
                        "llm_alert": (
                            f"[MOCK LLM] Cảnh báo an ninh: Phát hiện {count} người trong vùng giám sát!"
                            if count > 0
                            else ""
                        ),
                        "ai_active": True,
                    }
                self.mqtt_gateway.publish_telemetry(payload)
            return

        assert self._agent is not None
        try:
            for ai_payload in self._agent.run_inference_loop():
                if self._stop_event.is_set():
                    break
                self.mqtt_gateway.publish_telemetry(ai_payload)
        except Exception as exc:
            logger.exception("Luồng VisionAgent gặp lỗi: %s", exc)

    def run(self) -> None:
        cfg = self.config_manager.get()
        self.mqtt_gateway.start()

        ai_thread = threading.Thread(target=self._ai_worker_loop, name="AI-Telemetry-Worker", daemon=True)
        ai_thread.start()

        server_config = uvicorn.Config(
            app=self.web_app,
            host=cfg.webserver_host,
            port=cfg.webserver_port,
            log_level="warning",
        )
        server = uvicorn.Server(server_config)

        def _graceful_shutdown(signum, frame):
            logger.info("Nhận tín hiệu dừng (%s). Đang giải phóng tài nguyên Gateway...", signum)
            self._stop_event.set()
            server.should_exit = True

        signal.signal(signal.SIGINT, _graceful_shutdown)
        signal.signal(signal.SIGTERM, _graceful_shutdown)

        logger.info("Local Webserver T07 sẵn sàng tại http://localhost:%d", cfg.webserver_port)
        try:
            server.run()
        finally:
            self._stop_event.set()
            self.mqtt_gateway.stop()
            logger.info("IoT Gateway đã dừng hoàn toàn.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IoT PC Gateway (T08 + T07 + VisionAgent)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Chạy giả lập dữ liệu AI mỗi 5s mà không cần mở Webcam thật",
    )
    args = parser.parse_args()

    orchestrator = GatewayOrchestrator(mock_mode=args.mock)
    orchestrator.run()