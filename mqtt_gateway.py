"""
mqtt_gateway.py – Thành phần T08: IoT Gateway MQTT Client (paho-mqtt)
====================================================================
Tuân thủ 100% Hợp đồng Dữ liệu & Topic MQTT Gateway API của CoreIOT:
  • Connect device    : v1/gateway/connect    -> {"device": "Camera_AI_01"}
  • Disconnect device : v1/gateway/disconnect -> {"device": "Camera_AI_01"}
  • Publish telemetry : v1/gateway/telemetry  -> {"Camera_AI_01": [{"ts": ..., "values": {...}}]}
  • Subscribe RPC     : v1/gateway/rpc
  • Reply RPC         : v1/gateway/rpc/response/{request_id} (và v1/gateway/rpc)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import paho.mqtt.client as mqtt

from config import GatewayConfig
from state_store import StateStore
from vision_agent import AIPayloadSchema

logger = logging.getLogger("CoreIOTGateway")


class CoreIOTGatewayClient:
    """MQTT Gateway Bridge giữa Edge AI (TV1) và CoreIOT Cloud (TV3)."""

    TOPIC_CONNECT = "v1/gateway/connect"
    TOPIC_DISCONNECT = "v1/gateway/disconnect"
    TOPIC_TELEMETRY = "v1/gateway/telemetry"
    TOPIC_RPC = "v1/gateway/rpc"
    TOPIC_RPC_RESPONSE_PREFIX = "v1/gateway/rpc/response"

    def __init__(
        self,
        config: GatewayConfig,
        state_store: StateStore,
        on_set_ai_status: Callable[[bool], None],
        on_get_ai_status: Callable[[], bool],
    ) -> None:
        self._config = config
        self._store = state_store
        self._on_set_ai_status = on_set_ai_status
        self._on_get_ai_status = on_get_ai_status

        self._lock = threading.Lock()
        self._client: mqtt.Client | None = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def start(self) -> None:
        """Khởi tạo kết nối MQTT không đồng bộ tới CoreIOT."""
        with self._lock:
            self._stop_locked()
            cfg = self._config

            if not cfg.gateway_access_token or cfg.gateway_access_token.startswith("YOUR_"):
                msg = "Chưa cấu hình GATEWAY_ACCESS_TOKEN (chạy chế độ Offline Local)."
                logger.warning(msg)
                self._store.set_mqtt_status(False, msg)
                return

            client = self._create_paho_client(client_id=f"{cfg.gateway_name}_{int(time.time())}")
            client.username_pw_set(username=cfg.gateway_access_token)
            client.reconnect_delay_set(min_delay=2, max_delay=30)

            client.on_connect = self._handle_connect
            client.on_disconnect = self._handle_disconnect
            client.on_message = self._handle_message

            self._client = client
            try:
                logger.info(
                    "Đang kết nối CoreIOT MQTT Broker %s:%d (Gateway=%s)...",
                    cfg.coreiot_host,
                    cfg.coreiot_port,
                    cfg.gateway_name,
                )
                client.connect_async(cfg.coreiot_host, cfg.coreiot_port, keepalive=60)
                client.loop_start()
            except Exception as exc:
                err_msg = f"Lỗi khởi tạo kết nối MQTT: {exc}"
                logger.error(err_msg)
                self._store.set_mqtt_status(False, err_msg)

    def stop(self) -> None:
        """Gửi tín hiệu ngắt kết nối thiết bị con và dừng vòng lặp MQTT."""
        with self._lock:
            self._stop_locked()

    def reload(self, new_config: GatewayConfig) -> None:
        """Áp dụng cấu hình mới (khi lưu từ Webserver T07)."""
        with self._lock:
            self._config = new_config
        logger.info("Đang nạp lại kết nối CoreIOT MQTT với cấu hình mới...")
        self.start()

    def _stop_locked(self) -> None:
        if self._client is not None:
            try:
                if self._connected:
                    disconnect_payload = json.dumps({"device": self._config.device_name})
                    self._client.publish(self.TOPIC_DISCONNECT, disconnect_payload, qos=1)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
            finally:
                self._client = None
                self._connected = False
                self._store.set_mqtt_status(False, "Đã ngắt kết nối")

    @staticmethod
    def _create_paho_client(client_id: str) -> mqtt.Client:
        """Tương thích cả paho-mqtt v2.x lẫn v1.x."""
        if hasattr(mqtt, "CallbackAPIVersion"):
            return mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        return mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

    # ── MQTT Callbacks ──────────────────────────────────────────────────

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        rc = getattr(reason_code, "value", reason_code)
        if rc == 0:
            with self._lock:
                self._connected = True
                device_name = self._config.device_name
                host = self._config.coreiot_host

            self._store.set_mqtt_status(True, f"Đã kết nối ({host})")
            logger.info("Kết nối CoreIOT thành công! Đang đăng ký thiết bị con '%s'...", device_name)

            # 1. Báo cáo thiết bị con online qua Gateway API
            # [TV3] Gui kem "type" de CoreIOT tu gan Device profile "Camera_AI" (RuleChain T12/T13)
            connect_payload = json.dumps({"device": device_name, "type": "Camera_AI"}, ensure_ascii=False)
            client.publish(self.TOPIC_CONNECT, connect_payload, qos=1)

            # 2. Lắng nghe lệnh RPC từ CoreIOT Dashboard
            client.subscribe(self.TOPIC_RPC, qos=1)
            logger.info("Đã subscribe topic RPC: %s", self.TOPIC_RPC)
        else:
            err = f"CoreIOT từ chối kết nối (rc={rc}) – Kiểm tra Access Token!"
            logger.error(err)
            with self._lock:
                self._connected = False
            self._store.set_mqtt_status(False, err)

    def _handle_disconnect(self, client, userdata, disconnect_flags, reason_code=None, properties=None) -> None:
        with self._lock:
            self._connected = False
        self._store.set_mqtt_status(False, "Mất kết nối Broker – Đang thử kết nối lại...")
        logger.warning("Mất kết nối MQTT tới CoreIOT.")

    def _handle_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        """Xử lý bản tin RPC từ CoreIOT gửi xuống v1/gateway/rpc."""
        try:
            raw_text = msg.payload.decode("utf-8")
            logger.info("Nhận bản tin RPC [%s]: %s", msg.topic, raw_text)
            envelope = json.loads(raw_text)
        except Exception as exc:
            logger.error("Bản tin RPC không hợp lệ: %s", exc)
            return

        device_name = envelope.get("device", self._config.device_name)
        data = envelope.get("data", envelope)
        request_id = data.get("id")
        method = data.get("method")
        params = data.get("params")

        if not method:
            logger.warning("Bản tin RPC thiếu trường 'method': %s", envelope)
            return

        response_value = self._dispatch_rpc(method=method, params=params)

        if request_id is not None:
            self._send_rpc_response(
                client=client,
                device_name=device_name,
                request_id=request_id,
                response_value=response_value,
            )

        self._store.record_rpc(
            request_id=str(request_id if request_id is not None else "N/A"),
            device_name=device_name,
            method=method,
            params=params,
            response_payload=response_value,
        )

    # ── RPC Dispatch & Response ─────────────────────────────────────────

    @staticmethod
    def _parse_bool_param(params: object) -> bool:
        """Chuẩn hóa tham số RPC từ CoreIOT Switch Widget sang bool."""
        if isinstance(params, bool):
            return params
        if isinstance(params, (int, float)):
            return bool(params)
        if isinstance(params, str):
            normalized = params.strip().lower()
            if normalized in {"true", "1", "on", "yes"}:
                return True
            if normalized in {"false", "0", "off", "no"}:
                return False
        if isinstance(params, dict):
            for key in ("status", "value", "enabled", "state"):
                if key in params:
                    return CoreIOTGatewayClient._parse_bool_param(params[key])
        raise ValueError(f"Không thể chuyển đổi params={params!r} sang boolean")

    def _dispatch_rpc(self, method: str, params: object) -> bool:
        """Thực thi RPC method và trả về trạng thái boolean hiện tại của AI."""
        if method == "set_ai_status":
            target_status = self._parse_bool_param(params)
            self._on_set_ai_status(target_status)
            current_status = bool(self._on_get_ai_status())
            logger.info("RPC set_ai_status(%s) -> Trạng thái thực tế: %s", target_status, current_status)
            return current_status

        if method == "get_ai_status":
            current_status = bool(self._on_get_ai_status())
            logger.info("RPC get_ai_status() -> Trạng thái hiện tại: %s", current_status)
            return current_status

        logger.warning("RPC method không được hỗ trợ: %s", method)
        return bool(self._on_get_ai_status())

    def _send_rpc_response(
        self,
        client: mqtt.Client,
        device_name: str,
        request_id: int | str,
        response_value: bool,
    ) -> None:
        """Gửi phản hồi boolean về CoreIOT để tránh timeout trên Switch Widget."""
        # 1. Theo đúng Mục 4 bản đặc tả dữ liệu của nhóm:
        spec_topic = f"{self.TOPIC_RPC_RESPONSE_PREFIX}/{request_id}"
        spec_payload = json.dumps(response_value)
        client.publish(spec_topic, spec_payload, qos=1)

        # 2. Theo chuẩn Gateway RPC Reply của ThingsBoard/CoreIOT Broker:
        tb_gateway_reply = json.dumps(
            {
                "device": device_name,
                "id": request_id,
                "data": response_value,
            },
            ensure_ascii=False,
        )
        client.publish(self.TOPIC_RPC, tb_gateway_reply, qos=1)

        logger.info(
            "Đã gửi RPC response (id=%s, value=%s) tới '%s' & '%s'",
            request_id,
            response_value,
            spec_topic,
            self.TOPIC_RPC,
        )

    # ── Telemetry Packaging & Publishing ────────────────────────────────

    def build_gateway_telemetry(self, ai_payload: dict) -> tuple[dict, dict]:
        """Xác thực AIPayloadSchema và bọc vào cấu trúc JSON chuẩn của CoreIOT Gateway."""
        validated = AIPayloadSchema.model_validate(ai_payload)
        values = validated.model_dump()
        ts_ms = int(time.time() * 1000)

        with self._lock:
            device_name = self._config.device_name

        envelope = {
            device_name: [
                {
                    "ts": ts_ms,
                    "values": values,
                }
            ]
        }
        return values, envelope

    def publish_telemetry(self, ai_payload: dict) -> dict:
        """Đóng gói và đẩy chuỗi Telemetry lên topic v1/gateway/telemetry."""
        values, envelope = self.build_gateway_telemetry(ai_payload)
        device_name = next(iter(envelope.keys()))
        json_str = json.dumps(envelope, ensure_ascii=False)

        sent = False
        with self._lock:
            client = self._client
            connected = self._connected

        if client is not None and connected:
            info = client.publish(self.TOPIC_TELEMETRY, json_str, qos=1)
            sent = info.rc == mqtt.MQTT_ERR_SUCCESS
            if sent:
                logger.info("Đã đẩy Telemetry lên %s: %s", self.TOPIC_TELEMETRY, json_str)
            else:
                logger.error("Publish Telemetry thất bại (rc=%s)", info.rc)
        else:
            logger.debug("Gateway đang Offline – lưu Telemetry vào SQLite cục bộ: %s", json_str)

        self._store.record_telemetry(
            device_name=device_name,
            values=values,
            gateway_envelope=envelope,
            sent_to_cloud=sent,
        )
        return envelope