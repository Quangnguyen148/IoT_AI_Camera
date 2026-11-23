"""
gateway_simulator.py – Giả lập BTL_PC_GATEWAY để TV3 kiểm thử CoreIOT độc lập (T15, T11, T12, T13)
=================================================================================================
Gửi đúng Hợp đồng dữ liệu của nhóm qua MQTT Gateway API, không cần Webcam / YOLO / Gemini:
  • v1/gateway/connect   {"device": "Camera_AI_01"}
  • v1/gateway/telemetry {"Camera_AI_01": [{"ts": ..., "values": {people_count, anomaly_detected, llm_alert}}]}
  • v1/gateway/rpc       nhận set_ai_status / get_ai_status và trả lời đúng chuẩn

Ví dụ:
    pip install paho-mqtt
    # Chạy liên tục mỗi 5s, số người ngẫu nhiên 0..3
    python tools/gateway_simulator.py --token <ACCESS_TOKEN>

    # Kiểm thử RuleChain ban ngày: gán giờ giả 23h (khung giờ cấm) & luôn có 2 người
    python tools/gateway_simulator.py --token <ACCESS_TOKEN> --people 2 --fake-hour 23 --count 3

    # Sau đó gửi 0 người để rule chain tự Clear Alarm
    python tools/gateway_simulator.py --token <ACCESS_TOKEN> --people 0 --fake-hour 23 --count 1
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

VN_TZ = timezone(timedelta(hours=7))


def fake_ts_for_hour(hour: int) -> int:
    """Timestamp (ms) của hôm nay (giờ VN) tại `hour`:MM:SS hiện tại – dùng để test khung giờ cấm.

    Nếu thời điểm đó nằm ở tương lai thì lùi về hôm qua (CoreIOT vẫn chấp nhận dữ liệu quá khứ).
    """
    now = datetime.now(VN_TZ)
    target = now.replace(hour=hour)
    if target > now:
        target -= timedelta(days=1)
    return int(target.timestamp() * 1000)


def build_payload(device: str, people: int, ai_on: bool, ts: int) -> dict:
    if not ai_on:
        people = 0
    values = {
        "people_count": people,
        "anomaly_detected": people > 0,
        "llm_alert": (
            f"[SIMULATOR] Cảnh báo: Phát hiện {people} người trong vùng giám sát!" if people > 0 else ""
        ),
    }
    return {device: [{"ts": ts, "values": values}]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Giả lập BTL_PC_GATEWAY cho CoreIOT")
    ap.add_argument("--host", default=os.getenv("COREIOT_HOST", "app.coreiot.io"))
    ap.add_argument("--port", type=int, default=int(os.getenv("COREIOT_PORT", "1883")))
    ap.add_argument("--token", default=os.getenv("GATEWAY_ACCESS_TOKEN"), required=os.getenv("GATEWAY_ACCESS_TOKEN") is None)
    ap.add_argument("--device", default="Camera_AI_01")
    ap.add_argument("--device-type", default=None,
                    help="Tuỳ chọn: gửi kèm 'type' khi connect (vd Camera_AI) để CoreIOT tự gán Device profile")
    ap.add_argument("--people", type=int, default=None, help="Số người cố định (mặc định: ngẫu nhiên 0..3)")
    ap.add_argument("--fake-hour", type=int, default=None, help="Giả lập giờ VN của bản tin (0..23)")
    ap.add_argument("--interval", type=float, default=5.0, help="Chu kỳ gửi (giây) – Causal Window = 5s")
    ap.add_argument("--count", type=int, default=0, help="Số bản tin cần gửi (0 = chạy mãi)")
    args = ap.parse_args()

    state = {"ai_on": True}
    connected = threading.Event()

    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"BTL_SIM_{int(time.time())}")
    else:
        client = mqtt.Client(client_id=f"BTL_SIM_{int(time.time())}")
    client.username_pw_set(args.token)

    def on_connect(c, userdata, flags, rc, properties=None):
        rc = getattr(rc, "value", rc)
        if rc != 0:
            print(f"✖ CoreIOT từ chối kết nối (rc={rc}) – kiểm tra Access Token của BTL_PC_GATEWAY")
            return
        connect_msg = {"device": args.device}
        if args.device_type:
            connect_msg["type"] = args.device_type
        c.publish("v1/gateway/connect", json.dumps(connect_msg), qos=1)
        c.subscribe("v1/gateway/rpc", qos=1)
        print(f"✔ Đã kết nối {args.host}:{args.port} – đăng ký thiết bị con {connect_msg}")
        connected.set()

    def on_message(c, userdata, msg):
        try:
            env = json.loads(msg.payload.decode("utf-8"))
        except ValueError:
            return
        data = env.get("data", {})
        method, params, rid = data.get("method"), data.get("params"), data.get("id")
        if method == "set_ai_status":
            state["ai_on"] = params in (True, "true", 1, "1", "on")
        result = state["ai_on"]
        print(f"⇣ RPC {method}({params}) id={rid} -> trả lời {result}")
        if rid is not None:
            c.publish("v1/gateway/rpc", json.dumps({"device": env.get("device", args.device),
                                                    "id": rid, "data": result}), qos=1)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)
    client.loop_start()

    if not connected.wait(15):
        print("✖ Không kết nối được tới CoreIOT trong 15s")
        client.loop_stop()
        return 1

    sent = 0
    try:
        while args.count == 0 or sent < args.count:
            people = args.people if args.people is not None else random.choice([0, 0, 1, 2, 3])
            ts = fake_ts_for_hour(args.fake_hour) if args.fake_hour is not None else int(time.time() * 1000)
            payload = build_payload(args.device, people, state["ai_on"], ts)
            client.publish("v1/gateway/telemetry", json.dumps(payload, ensure_ascii=False), qos=1)
            hour = datetime.fromtimestamp(ts / 1000, VN_TZ).strftime("%H:%M")
            print(f"⇡ Telemetry [{hour} VN] {json.dumps(payload[args.device][0]['values'], ensure_ascii=False)}")
            sent += 1
            if args.count and sent >= args.count:
                break
            time.sleep(args.interval)
        time.sleep(2 if args.count else 0)  # chờ thêm để nhận RPC / flush QoS1
    except KeyboardInterrupt:
        pass
    finally:
        client.publish("v1/gateway/disconnect", json.dumps({"device": args.device}), qos=1)
        time.sleep(0.5)
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
