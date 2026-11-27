"""
mqtt_gateway.py – Thành phần T08: IoT Gateway MQTT Client (paho-mqtt)
====================================================================
Tuân thủ 100% Hợp đồng Dữ liệu & Topic MQTT Gateway API của CoreIOT:
  • Connect device    : v1/gateway/connect    -> {"device": "Camera_AI_01", "type": "Camera_AI"}
  • Disconnect device : v1/gateway/disconnect -> {"device": "Camera_AI_01"}
  • Publish telemetry : v1/gateway/telemetry  -> {"Camera_AI_01": [{"ts": ..., "values": {...}}]}
  • Publish attributes: v1/gateway/attributes -> {"Camera_AI_01": {"ai_active": bool}}
  • Subscribe RPC     : v1/gateway/rpc & v1/devices/me/rpc/request/+
  • Reply RPC         : v1/gateway/rpc/response/{request_id} & v1/gateway/rpc & v1/devices/me/rpc/response/{request_id}
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
    TOPIC_ATTRIBUTES = "v1/gateway/attributes"
    TOPIC_RPC = "v1/gateway/rpc"
    TOPIC_DEVICE_RPC_REQUEST = "v1/devices/me/rpc/request/+"
    TOPIC_DEVICE_RPC_RESPONSE_PREFIX = "v1/devices/me/rpc/response"
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
        self._last_published_ai_status: bool | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def start(self) -> None:
        """Khởi tạo kết nối MQTT không đồng bộ tới CoreIOT (an toàn không gây deadlock)."""
        self.stop()

        with self._lock:
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
        """Gửi tín hiệu ngắt kết nối thiết bị con và dừng vòng lặp MQTT ngoài lock để tránh deadlock."""
        client_to_stop: mqtt.Client | None = None
        device_name: str = ""
        was_connected: bool = False

        with self._lock:
            if self._client is not None:
                client_to_stop = self._client
                was_connected = self._connected
                device_name = self._config.device_name
                self._client = None
                self._connected = False
                self._store.set_mqtt_status(False, "Đã ngắt kết nối")

        if client_to_stop is not None:
            try:
                if was_connected:
                    disconnect_payload = json.dumps({"device": device_name})
                    client_to_stop.publish(self.TOPIC_DISCONNECT, disconnect_payload, qos=1)
                client_to_stop.disconnect()
                client_to_stop.loop_stop()
            except Exception as exc:
                logger.debug("Lỗi khi dừng MQTT Client: %s", exc)

    def reload(self, new_config: GatewayConfig) -> None:
        """Áp dụng cấu hình mới: chỉ kết nối lại MQTT nếu thông số broker/gateway thay đổi và chạy bất đồng bộ."""
        mqtt_changed = False
        with self._lock:
            old = self._config
            self._config = new_config
            mqtt_changed = (
                old.coreiot_host != new_config.coreiot_host
                or old.coreiot_port != new_config.coreiot_port
                or old.gateway_name != new_config.gateway_name
                or old.device_name != new_config.device_name
                or old.gateway_access_token != new_config.gateway_access_token
            )

        if not mqtt_changed:
            logger.info("Thông số kết nối MQTT không đổi – Giữ nguyên kết nối Broker hiện tại.")
            return

        logger.info("Phát hiện thay đổi cấu hình kết nối MQTT. Đang tái khởi động MQTT ở background...")
        reconnect_thread = threading.Thread(
            target=self.start,
            name="MQTT-HotReload-Worker",
            daemon=True,
        )
        reconnect_thread.start()

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
            connect_payload = json.dumps({"device": device_name, "type": "Camera_AI"}, ensure_ascii=False)
            client.publish(self.TOPIC_CONNECT, connect_payload, qos=1)

            # 2. Lắng nghe lệnh RPC từ CoreIOT Dashboard (Cả subdevice RPC lẫn Direct Device RPC)
            client.subscribe([(self.TOPIC_RPC, 1), (self.TOPIC_DEVICE_RPC_REQUEST, 1)])
            logger.info("Đã subscribe topics RPC: %s và %s", self.TOPIC_RPC, self.TOPIC_DEVICE_RPC_REQUEST)

            # 3. Đồng bộ trạng thái AI hiện tại lên CoreIOT ngay khi vừa kết nối (force=True)
            current_ai = bool(self._on_get_ai_status())
            self.publish_ai_status(current_ai, force=True)
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
        """Xử lý bản tin RPC từ CoreIOT gửi xuống v1/gateway/rpc hoặc v1/devices/me/rpc/request/+."""
        try:
            raw_text = msg.payload.decode("utf-8")
            logger.info("Nhận bản tin MQTT [%s]: %s", msg.topic, raw_text)
            envelope = json.loads(raw_text)
        except Exception as exc:
            logger.error("Bản tin MQTT không phải JSON hợp lệ trên topic [%s]: %s", msg.topic, exc)
            return

        try:
            is_direct_rpc = msg.topic.startswith("v1/devices/me/rpc/request/")
            if is_direct_rpc:
                request_id = msg.topic.split("/")[-1]
                with self._lock:
                    device_name = self._config.gateway_name
                method = envelope.get("method") if isinstance(envelope, dict) else None
                params = envelope.get("params") if isinstance(envelope, dict) else None
            else:
                # v1/gateway/rpc
                if not isinstance(envelope, dict):
                    logger.debug("Bản tin v1/gateway/rpc không phải JSON object: %r", envelope)
                    return

                with self._lock:
                    default_dev = self._config.device_name
                device_name = envelope.get("device", default_dev)
                raw_data = envelope.get("data")

                # Bỏ qua nếu đây là phản hồi do chính Gateway gửi lên v1/gateway/rpc (data là boolean/primitive, không có method)
                if not isinstance(raw_data, dict):
                    if "method" in envelope:
                        data = envelope
                    else:
                        logger.debug("Bỏ qua bản tin phản hồi/echo trên %s: %s", msg.topic, raw_text)
                        return
                else:
                    data = raw_data

                request_id = data.get("id") if "id" in data else envelope.get("id")
                method = data.get("method") if "method" in data else envelope.get("method")
                params = data.get("params") if "params" in data else envelope.get("params")

            if not method:
                logger.debug("Bản tin không chứa trường 'method' hợp lệ (bỏ qua): %s", raw_text)
                return

            logger.info(
                "[RPC ĐẾN] device=%s | method=%s | id=%s | params=%s",
                device_name,
                method,
                request_id,
                params,
            )
            response_value = self._dispatch_rpc(method=method, params=params)

            if request_id is not None:
                self._send_rpc_response(
                    client=client,
                    device_name=device_name,
                    request_id=request_id,
                    response_value=response_value,
                    is_direct_rpc=is_direct_rpc,
                )

            self._store.record_rpc(
                request_id=str(request_id if request_id is not None else "N/A"),
                device_name=device_name,
                method=method,
                params=params,
                response_payload=response_value,
            )
        except Exception as exc:
            logger.exception("Lỗi khi xử lý bản tin RPC: %s", exc)

    # ── RPC Dispatch & Response ─────────────────────────────────────────

    @staticmethod
    def _parse_bool_param(params: object) -> bool:
        """Chuẩn hóa tham số RPC từ CoreIOT Switch Widget sang bool an toàn."""
        if isinstance(params, bool):
            return params
        if isinstance(params, (int, float)):
            return bool(params)
        if isinstance(params, str):
            normalized = params.strip().lower()
            if normalized in {"true", "1", "on", "yes", "active", "enabled"}:
                return True
            if normalized in {"false", "0", "off", "no", "inactive", "disabled"}:
                return False
        if isinstance(params, dict):
            for key in ("status", "value", "enabled", "state", "ai_active", "ai_status", "active", "params"):
                if key in params:
                    return CoreIOTGatewayClient._parse_bool_param(params[key])
        return False

    def _dispatch_rpc(self, method: str, params: object) -> bool:
        """Thực thi RPC method và trả về trạng thái boolean hiện tại của AI."""
        if method == "set_ai_status":
            target_status = self._parse_bool_param(params)
            try:
                self._on_set_ai_status(target_status, source="CoreIOT_RPC", sync_cloud=False)
            except TypeError:
                self._on_set_ai_status(target_status)
            current_status = bool(self._on_get_ai_status())
            logger.info(
                "[RPC THỰC THI] set_ai_status(%s) -> Trạng thái áp dụng thực tế: %s",
                target_status,
                current_status,
            )
            return current_status

        if method == "get_ai_status":
            current_status = bool(self._on_get_ai_status())
            logger.info("[RPC THỰC THI] get_ai_status() -> Trạng thái hiện tại: %s", current_status)
            return current_status

        logger.warning("[RPC THỰC THI] RPC method không được hỗ trợ: %s", method)
        return bool(self._on_get_ai_status())

    def _send_rpc_response(
        self,
        client: mqtt.Client,
        device_name: str,
        request_id: int | str,
        response_value: bool,
        is_direct_rpc: bool = False,
    ) -> None:
        """Gửi phản hồi boolean về CoreIOT để tránh timeout trên Switch Widget."""
        try:
            if is_direct_rpc:
                direct_topic = f"{self.TOPIC_DEVICE_RPC_RESPONSE_PREFIX}/{request_id}"
                client.publish(direct_topic, json.dumps(response_value), qos=1)
                logger.info("Đã gửi Direct RPC response tới '%s': %s", direct_topic, response_value)
                return

            # 1. Topic phản hồi theo đặc tả nhóm: v1/gateway/rpc/response/{request_id}
            spec_topic = f"{self.TOPIC_RPC_RESPONSE_PREFIX}/{request_id}"
            spec_payload = json.dumps(response_value)
            client.publish(spec_topic, spec_payload, qos=1)

            # 2. Topic phản hồi Gateway chuẩn của ThingsBoard CoreIOT Broker: v1/gateway/rpc
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
        except Exception as exc:
            logger.error("Lỗi khi gửi phản hồi RPC: %s", exc)

    # ── State Sync (Attributes) & Telemetry ─────────────────────────────

    def publish_ai_status(self, active: bool, force: bool = False) -> None:
        """Đồng bộ trạng thái AI lên CoreIOT Cloud qua Client Attributes và Telemetry tức thời (chặn echo/duplicate)."""
        status_bool = bool(active)
        with self._lock:
            client = self._client
            connected = self._connected
            device_name = self._config.device_name
            if not force and self._last_published_ai_status == status_bool:
                logger.debug("Trạng thái AI đã được đồng bộ lên Cloud (%s) – bỏ qua publish lặp.", status_bool)
                return
            self._last_published_ai_status = status_bool

        if client is None or not connected:
            logger.debug("Gateway đang Offline, chưa thể đồng bộ trạng thái AI lên Cloud.")
            return

        try:
            # 1. Đẩy Client Attribute lên v1/gateway/attributes (để Switch Widget tự bật/tắt realtime)
            attr_envelope = {
                device_name: {
                    "ai_active": bool(active),
                    "ai_status": bool(active),
                }
            }
            attr_json = json.dumps(attr_envelope, ensure_ascii=False)
            client.publish(self.TOPIC_ATTRIBUTES, attr_json, qos=1)
            logger.info("Đã đồng bộ Client Attribute lên %s: %s", self.TOPIC_ATTRIBUTES, attr_json)

            # 2. Đẩy kèm bản tin Telemetry để Dashboard timeseries cập nhật ngay
            ts_ms = int(time.time() * 1000)
            tel_envelope = {
                device_name: [
                    {
                        "ts": ts_ms,
                        "values": {
                            "ai_active": bool(active),
                            "people_count": 0 if not active else 0,
                            "anomaly_detected": False if not active else False,
                        },
                    }
                ]
            }
            tel_json = json.dumps(tel_envelope, ensure_ascii=False)
            client.publish(self.TOPIC_TELEMETRY, tel_json, qos=1)
            logger.info("Đã đồng bộ Telemetry tức thời trạng thái AI lên %s", self.TOPIC_TELEMETRY)
        except Exception as exc:
            logger.error("Lỗi khi đồng bộ trạng thái AI lên CoreIOT: %s", exc)

    def build_gateway_telemetry(self, ai_payload: dict) -> tuple[dict, dict]:
        """Xác thực AIPayloadSchema và bọc vào cấu trúc JSON chuẩn của CoreIOT Gateway."""
        validated = AIPayloadSchema.model_validate(ai_payload)
        values = validated.model_dump()
        # Luôn đọc trực tiếp từ trạng thái hiện hành để không bao giờ gửi đè giá trị cũ (stale cache)
        values["ai_active"] = bool(self._on_get_ai_status())
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
