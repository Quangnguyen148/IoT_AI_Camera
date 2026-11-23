"""
streamlit_app.py – Thành phần T10: Giao diện giám sát & điều khiển trên Gateway local
=====================================================================================
Chạy độc lập:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import json
import os
import time

import pandas as pd
import requests
import streamlit as st

from state_store import StateStore

API_BASE_URL = os.getenv("GATEWAY_API_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="IoT Edge Gateway Dashboard (T10)",
    page_icon="🛡️",
    layout="wide",
)


@st.cache_resource
def get_fallback_store() -> StateStore:
    return StateStore()


def fetch_status() -> tuple[dict, bool]:
    """Lấy trạng thái thời gian thực từ Gateway API; fallback sang SQLite nếu backend đang khởi động."""
    try:
        res = requests.get(f"{API_BASE_URL}/api/status", timeout=1.5)
        res.raise_for_status()
        return res.json(), True
    except Exception:
        snap = get_fallback_store().get_snapshot()
        snap["lan_url"] = API_BASE_URL
        snap["config"] = {}
        return snap, False


def fetch_logs(limit: int = 120) -> tuple[list[dict], list[dict]]:
    try:
        res = requests.get(f"{API_BASE_URL}/api/logs", params={"limit": limit}, timeout=1.5)
        res.raise_for_status()
        data = res.json()
        return data.get("telemetry", []), data.get("rpcs", [])
    except Exception:
        store = get_fallback_store()
        return store.get_recent_telemetry(limit=limit), store.get_recent_rpcs(limit=50)


def send_ai_toggle(target_status: bool) -> bool:
    try:
        res = requests.post(
            f"{API_BASE_URL}/api/ai/toggle",
            json={"status": target_status},
            timeout=2.0,
        )
        res.raise_for_status()
        return True
    except Exception as exc:
        st.error(f"Không thể gửi lệnh tới Gateway ({API_BASE_URL}): {exc}")
        return False


# ── Lấy dữ liệu trạng thái & lịch sử ─────────────────────────────────────
status, backend_online = fetch_status()
telemetry_logs, rpc_logs = fetch_logs(limit=150)

ai_active: bool = bool(status.get("ai_active", True))
mqtt_connected: bool = bool(status.get("mqtt_connected", False))
latest_tel: dict = status.get("latest_telemetry", {})
people_count: int = int(latest_tel.get("people_count", 0))
anomaly_detected: bool = bool(latest_tel.get("anomaly_detected", False))
llm_alert: str = str(latest_tel.get("llm_alert", ""))

# ── Sidebar Điều khiển ───────────────────────────────────────────────────
with st.sidebar:
    st.header("Điều khiển Gateway")
    if backend_online:
        st.success("Gateway Service: ONLINE")
    else:
        st.warning("Gateway Service: OFFLINE (Hãy chạy `python gateway_main.py`)")

    st.divider()
    st.subheader("Module Edge AI (T09)")
    new_ai_state = st.toggle(
        "Kích hoạt nhận diện & cảnh báo AI",
        value=ai_active,
        disabled=not backend_online,
        help="Đồng bộ trực tiếp với VisionAgent và lệnh RPC set_ai_status từ CoreIOT.",
    )
    if backend_online and new_ai_state != ai_active:
        if send_ai_toggle(new_ai_state):
            st.rerun()

    st.divider()
    auto_refresh = st.checkbox("Tự động cập nhật (mỗi 2 giây)", value=True)
    if st.button("Làm mới dữ liệu ngay", width="stretch"):
        st.rerun()

    st.divider()
    lan_url = status.get("lan_url", API_BASE_URL)
    st.markdown(f"**Local Webserver (T07):** [{lan_url}]({lan_url})")
    cfg = status.get("config", {})
    if cfg:
        st.caption(
            f"Gateway: `{cfg.get('gateway_name', 'BTL_PC_GATEWAY')}` | "
            f"Device: `{cfg.get('device_name', 'Camera_AI_01')}`\n\n"
            f"Quarantine: `conf > {cfg.get('conf_threshold', 0.6)}` × `{cfg.get('quarantine_frames', 15)} frames` | "
            f"Window: `{cfg.get('causal_window_sec', 5.0)}s`"
        )

# ── Header & 4 Thẻ KPI ───────────────────────────────────────────────────
st.title("IoT Edge Gateway – Bảng Điều Khiển Giám Sát Cục Bộ (T10)")
st.caption("Giám sát số lượng người thời gian thực, log cảnh báo Gemini LLM và trạng thái đồng bộ CoreIOT Cloud.")

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric(
        label="Số người trong khung hình",
        value=f"{people_count} người",
        delta="Phát hiện xâm nhập" if people_count > 0 else "Khu vực trống",
        delta_color="inverse" if people_count > 0 else "normal",
    )
with col2:
    st.metric(
        label="Trạng thái Module AI",
        value="ĐANG HOẠT ĐỘNG" if ai_active else "TẠM DỪNG",
    )
with col3:
    st.metric(
        label="Cờ An Ninh (Anomaly)",
        value="CẢNH BÁO" if anomaly_detected else "AN TOÀN",
    )
with col4:
    st.metric(
        label="Kết nối CoreIOT MQTT",
        value="ONLINE" if mqtt_connected else "OFFLINE",
        delta=f"Đã gửi {status.get('total_published', 0)} bản tin",
    )

# ── Khung hiển thị cảnh báo LLM mới nhất ─────────────────────────────────
if anomaly_detected and llm_alert:
    st.error(f"**CẢNH BÁO TỪ TRỢ LÝ ẢO LLM:** {llm_alert}")
elif not ai_active:
    st.info("Module AI đang tạm dừng theo lệnh điều khiển. Camera vẫn duy trì kết nối.")
else:
    st.success("Hệ thống đang giám sát bình thường. Không phát hiện người trong khung hình.")

# ── Các Tab Chi Tiết ─────────────────────────────────────────────────────
tab_monitor, tab_llm, tab_protocol = st.tabs(
    ["Biểu đồ & Camera Trực Tiếp", "Log Lịch Sử Cảnh Báo LLM", "Gói Tin JSON & Lịch Sử RPC"]
)

with tab_monitor:
    left_col, right_col = st.columns([1.3, 1.0])
    with left_col:
        st.subheader("Biến động số lượng người theo thời gian")
        if telemetry_logs:
            df = pd.DataFrame(telemetry_logs).sort_values("id")
            chart_df = df.set_index("iso_time")[["people_count"]]
            st.area_chart(chart_df, height=320)
        else:
            st.info("Chưa có dữ liệu lịch sử. Đợi chu kỳ Causal Window 5 giây đầu tiên...")

    with right_col:
        st.subheader("Luồng Video Webcam Trực Tiếp (T07)")
        if backend_online:
            st.markdown(
                f'<img src="{API_BASE_URL}/video_feed" style="width:100%; border-radius:10px; border:1px solid #334155;" />',
                unsafe_allow_html=True,
            )
        else:
            st.info("Bật `gateway_main.py` để xem luồng video trực tiếp.")

with tab_llm:
    st.subheader("Nhật ký nhận xét & cảnh báo từ Gemini LLM (N01)")
    only_alerts = st.checkbox("Chỉ hiển thị các bản tin có phát hiện người / cảnh báo", value=True)

    filtered_logs = [
        item for item in telemetry_logs if (not only_alerts or item["people_count"] > 0 or item["llm_alert"])
    ]

    if filtered_logs:
        for item in filtered_logs[:25]:
            ts_label = item["iso_time"]
            cnt = item["people_count"]
            msg = item["llm_alert"] or "(Không có cảnh báo)"
            cloud_badge = "Đã đồng bộ CoreIOT" if item["sent_to_cloud"] else "Lưu cục bộ (Offline)"
            if cnt > 0:
                st.warning(f"**[{ts_label}]** | Số người: **{cnt}** | *{cloud_badge}*\n\n> {msg}")
            else:
                st.write(f"**[{ts_label}]** | Số người: **0** | *{cloud_badge}* — {msg}")
    else:
        st.info("Chưa có bản ghi cảnh báo nào từ LLM.")

with tab_protocol:
    c_json, c_rpc = st.columns(2)
    with c_json:
        st.subheader("Payload Telemetry mới nhất (v1/gateway/telemetry)")
        latest_json = status.get("latest_gateway_json") or {}
        if latest_json:
            st.code(json.dumps(latest_json, ensure_ascii=False, indent=2), language="json")
        else:
            st.info("Chưa có gói tin nào được đóng gói.")

    with c_rpc:
        st.subheader("Nhật ký lệnh điều khiển RPC (v1/gateway/rpc)")
        if rpc_logs:
            st.dataframe(pd.DataFrame(rpc_logs), width="stretch", hide_index=True)
        else:
            st.info("Chưa nhận lệnh RPC nào từ CoreIOT Dashboard.")

if auto_refresh:
    time.sleep(2.0)
    st.rerun()