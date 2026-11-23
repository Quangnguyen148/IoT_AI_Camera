"""
test_gateway.py – Kiểm thử tự động hợp đồng dữ liệu CoreIOT, RPC và Local Webserver
Chạy:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Đảm bảo import từ thư mục gốc hoạt động bình thường khi chạy từ bất kỳ thư mục nào
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from fastapi.testclient import TestClient
    from web_server import create_web_app
    HAS_FASTAPI = True
except ImportError:
    TestClient = None  # type: ignore
    create_web_app = None  # type: ignore
    HAS_FASTAPI = False

from config import ConfigManager, GatewayConfig
from mqtt_gateway import CoreIOTGatewayClient
from state_store import StateStore


class TestIoTGatewayMember2(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp_dir.name) / "test_state.db"
        self.cfg_path = Path(self.tmp_dir.name) / "test_config.json"

        self.store = StateStore(db_path=self.db_path)
        self.cfg_mgr = ConfigManager(json_path=self.cfg_path)
        self.ai_state = True

        def set_ai(val: bool) -> None:
            self.ai_state = bool(val)
            self.store.set_ai_active(self.ai_state)

        def get_ai() -> bool:
            return self.ai_state

        self.set_ai = set_ai
        self.get_ai = get_ai
        self.gateway = CoreIOTGatewayClient(
            config=self.cfg_mgr.get(),
            state_store=self.store,
            on_set_ai_status=self.set_ai,
            on_get_ai_status=self.get_ai,
        )

    def tearDown(self) -> None:
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_telemetry_contract_format(self) -> None:
        """Kiểm tra JSON đóng gói khớp tuyệt đối cấu trúc CoreIOT Gateway API."""
        ai_out = {
            "people_count": 2,
            "anomaly_detected": True,
            "llm_alert": "Cảnh báo: Có 2 người lạ xuất hiện trong phòng.",
        }
        values, envelope = self.gateway.build_gateway_telemetry(ai_out)

        self.assertIn("Camera_AI_01", envelope)
        self.assertEqual(len(envelope["Camera_AI_01"]), 1)
        record = envelope["Camera_AI_01"][0]
        self.assertIsInstance(record["ts"], int)
        self.assertGreater(record["ts"], 1_700_000_000_000)
        self.assertEqual(record["values"], values)
        self.assertEqual(record["values"]["people_count"], 2)
        self.assertTrue(record["values"]["anomaly_detected"])

    def test_connect_payload_device_type(self) -> None:
        """Kiểm tra bản tin v1/gateway/connect gửi kèm type: Camera_AI để CoreIOT tự gán profile."""
        mock_client = MagicMock()
        self.gateway._handle_connect(mock_client, None, {}, 0)
        expected_payload = json.dumps({"device": "Camera_AI_01", "type": "Camera_AI"}, ensure_ascii=False)
        mock_client.publish.assert_any_call("v1/gateway/connect", expected_payload, qos=1)

    def test_rpc_set_and_get_ai_status(self) -> None:
        """Kiểm tra luồng RPC set_ai_status và get_ai_status phản hồi boolean đúng topic."""
        mock_client = MagicMock()

        # 1. Gửi lệnh tắt AI (set_ai_status = false)
        msg_off = MagicMock()
        msg_off.topic = "v1/gateway/rpc"
        msg_off.payload = json.dumps(
            {"device": "Camera_AI_01", "data": {"id": 101, "method": "set_ai_status", "params": False}}
        ).encode("utf-8")

        self.gateway._handle_message(mock_client, None, msg_off)
        self.assertFalse(self.ai_state)
        mock_client.publish.assert_any_call("v1/gateway/rpc/response/101", "false", qos=1)

        # 2. Gửi lệnh hỏi trạng thái ban đầu (get_ai_status)
        msg_get = MagicMock()
        msg_get.topic = "v1/gateway/rpc"
        msg_get.payload = json.dumps(
            {"device": "Camera_AI_01", "data": {"id": 102, "method": "get_ai_status", "params": None}}
        ).encode("utf-8")

        self.gateway._handle_message(mock_client, None, msg_get)
        mock_client.publish.assert_any_call("v1/gateway/rpc/response/102", "false", qos=1)

    @unittest.skipUnless(HAS_FASTAPI, "Cần cài đặt fastapi và httpx để kiểm thử Webserver")
    def test_webserver_endpoints_and_config_reload(self) -> None:
        """Kiểm tra Webserver T07: API status, toggle AI và lưu cấu hình HTML."""
        app = create_web_app(
            config_manager=self.cfg_mgr,
            state_store=self.store,
            get_frame_fn=lambda: None,
            set_ai_status_fn=self.set_ai,
            get_ai_status_fn=self.get_ai,
        )
        client = TestClient(app)

        # Kiểm tra trang chủ HTML
        home_res = client.get("/")
        self.assertEqual(home_res.status_code, 200)
        self.assertIn("IoT Edge Gateway", home_res.text)

        # Kiểm tra API bật/tắt AI
        toggle_res = client.post("/api/ai/toggle", json={"status": False})
        self.assertEqual(toggle_res.status_code, 200)
        self.assertFalse(toggle_res.json()["ai_active"])
        self.assertFalse(self.ai_state)

        # Kiểm tra lưu cấu hình qua form HTML
        cfg_res = client.post(
            "/config",
            data={
                "coreiot_host": "demo.coreiot.io",
                "coreiot_port": "1883",
                "gateway_access_token": "TEST_TOKEN_123",
                "gemini_api_key": "",
                "conf_threshold": "0.75",
                "quarantine_frames": "20",
                "causal_window_sec": "5.0",
            },
            follow_redirects=False,
        )
        self.assertEqual(cfg_res.status_code, 303)
        updated = self.cfg_mgr.get()
        self.assertEqual(updated.coreiot_host, "demo.coreiot.io")
        self.assertEqual(updated.gateway_access_token, "TEST_TOKEN_123")
        self.assertEqual(updated.conf_threshold, 0.75)


if __name__ == "__main__":
    unittest.main()