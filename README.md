# BTL IoT – Hệ thống Giám sát An ninh Camera Edge AI & CoreIOT Cloud

Dự án tích hợp hoàn chỉnh hệ thống **Camera AI An ninh & IoT Gateway** kết nối nền tảng đám mây **CoreIOT Cloud (ThingsBoard)**, bao gồm công việc của 3 thành viên:
- **Thành viên 1 (Edge AI):** Nhận diện người qua YOLOv8n, cơ chế chống nhiễu kép (Quarantine Isolation + Causal Windowing 5s) và tạo cảnh báo ngữ nghĩa tự nhiên bằng Google Gemini LLM (`vision_agent.py`).
- **Thành viên 2 (Gateway & Local Services):** IoT Gateway MQTT Bridge theo chuẩn Gateway API (`mqtt_gateway.py`), Local Webserver cấu hình động & MJPEG Stream (`web_server.py`), và giao diện giám sát & điều khiển thời gian thực (`streamlit_app.py`).
- **Thành viên 3 (CoreIOT Cloud & Automation):** Thiết lập Gateway `BTL_PC_GATEWAY` tự sinh thiết bị con `Camera_AI_01`, Rule Chain `BTL Camera AI Security` lọc khung giờ cấm, cảnh báo Alarm & thông báo chuông đỏ Notification Center, cùng Cloud Dashboard điều khiển 2 chiều RPC.

---

## 1. Tổng quan & Kiến trúc hệ thống

### 1.1 Sơ đồ kiến trúc luồng dữ liệu

```text
       ┌────────────────────────────────────────────────────────┐
       │                EDGE AI LAYER (TV1)                     │
       │  Webcam ──► YOLOv8n (conf > 0.6, Quarantine 15 frames) │
       │                │                                       │
       │                ▼ Causal Window (5s)                    │
       │         Gemini 2.0 Flash LLM                           │
       │  {people_count, anomaly_detected, llm_alert}           │
       └────────────────────────┬───────────────────────────────┘
                                │
                                ▼
       ┌────────────────────────────────────────────────────────┐
       │               LOCAL GATEWAY LAYER (TV2)                │
       │  • gateway_main.py : Tiến trình điều phối trung tâm    │
       │  • state_store.py  : SQLite & Cache đồng bộ luồng      │
       │  • web_server.py   : FastAPI (Port 8000) MJPEG + Form  │
       │  • streamlit_app.py: Giám sát & Điều khiển (Port 8501) │
       │  • mqtt_gateway.py : MQTT Client (paho-mqtt)           │
       └────────────────────────┬───────────────────────────────┘
                                │ MQTT (Port 1883)
                                │ v1/gateway/connect  {"device":"Camera_AI_01", "type":"Camera_AI"}
                                │ v1/gateway/telemetry {"Camera_AI_01": [{ts, values}]}
                                │ v1/gateway/rpc  ◄──► set_ai_status / get_ai_status
                                ▼
       ┌────────────────────────────────────────────────────────┐
       │                COREIOT CLOUD LAYER (TV3)               │
       │  • Gateway Entity: BTL_PC_GATEWAY (Is gateway = true)  │
       │  • Device Entity : Camera_AI_01 (Profile: Camera_AI)   │
       │  • Rule Chain    : "BTL Camera AI Security"            │
       │      Save Timeseries ──► Server Attr (khung giờ cấm)   │
       │      ──► Script Filter ──► Create Alarm (Critical)     │
       │      ──► Send Notification                             │
       │      ──► Clear Alarm (khi rời khỏi khung cấm / 0 người)│
       │  • Dashboard     : Thẻ người, Trạng thái, Biểu đồ,     │
       │                    Switch RPC 2 chiều, Bảng Alarm & LLM│
       └────────────────────────────────────────────────────────┘
```

### 1.2 Hợp đồng dữ liệu (Data Contract)
Hệ thống tuân thủ nghiêm ngặt chuẩn **CoreIOT MQTT Gateway API**:
1. **Đăng ký thiết bị con (`v1/gateway/connect`):**
   ```json
   {"device": "Camera_AI_01", "type": "Camera_AI"}
   ```
   *(Nhờ trường `"type": "Camera_AI"`, CoreIOT tự động gán thiết bị vào Device Profile `Camera_AI` ngay khi online, lập tức kích hoạt RuleChain bảo vệ).*
2. **Bản tin định kỳ mỗi 5s (`v1/gateway/telemetry`):**
   ```json
   {
     "Camera_AI_01": [
       {
         "ts": 1700000000000,
         "values": {
           "people_count": 2,
           "anomaly_detected": true,
           "llm_alert": "Cảnh báo: Có 2 người lạ xuất hiện trong vùng quan sát."
         }
       }
     ]
   }
   ```
3. **Điều khiển RPC hai chiều (`v1/gateway/rpc`):**
   - Cloud gửi xuống Gateway:
     ```json
     {"device": "Camera_AI_01", "data": {"id": 1, "method": "set_ai_status", "params": false}}
     ```
   - Gateway phản hồi xác nhận:
     ```json
     {"device": "Camera_AI_01", "id": 1, "data": false}
     ```

### 1.3 Cấu trúc thư mục hoàn chỉnh

```text
BTL_IoT_Camera_AI/
├── vision_agent.py       # [TV1] Module Edge AI (YOLOv8n + Quarantine + Gemini LLM)
├── yolov8n.pt            # [TV1] Trọng số mô hình YOLOv8n tiền huấn luyện
├── config.py             # Quản lý cấu hình (.env + gateway_config.json) & hot-reload
├── state_store.py        # Lưu trữ trạng thái SQLite & Audit Log thời gian thực
├── mqtt_gateway.py       # [T08] CoreIOT MQTT Gateway Client (đã vá type: Camera_AI)
├── web_server.py         # [T07] Local Webserver FastAPI (MJPEG stream + Form cấu hình)
├── streamlit_app.py      # [T10] Giao diện Dashboard cục bộ (Streamlit)
├── gateway_main.py       # Tiến trình lõi tích hợp VisionAgent + Gateway + Webserver
├── run_all.py            # Script khởi chạy toàn bộ dịch vụ local bằng 1 lệnh
├── coreiot/              # [TV3] Cấu hình xuất xưởng CoreIOT (Rule Chain, Dashboard, Templates)
│   ├── rulechain_btl_camera_ai_security.json  # RuleChain lọc khung giờ cấm & báo động
│   ├── dashboard_btl_camera_ai.json           # Dashboard giám sát & điều khiển
│   ├── notification_template.json             # Mẫu thông báo chuông đỏ
│   └── scripts/                               # Các script JavaScript nhúng trong Rule Node
├── tools/                # [TV3] Công cụ bổ trợ thiết lập & giả lập Cloud
│   ├── coreiot_setup.py        # Tự động hóa cấu hình CoreIOT qua REST API
│   ├── gateway_simulator.py    # Giả lập Gateway gửi telemetry/RPC để test Cloud độc lập
│   ├── coreiot_templates.py    # Thư viện sinh JSON template CoreIOT
│   └── export_templates.py     # Xuất lại các file trong coreiot/ từ template
├── tests/                # Bộ kiểm thử tự động toàn diện
│   ├── test_gateway.py         # Test hợp đồng Gateway, RPC & Webserver (TV2)
│   └── test_tv3.py             # Test logic RuleChain, template JSON & setup API (TV3)
├── .env.example          # File mẫu cấu hình biến môi trường phân nhóm
├── .gitignore            # Bỏ qua file nhạy cảm, cache và database
└── requirements.txt      # Toàn bộ danh sách phụ thuộc Python
```

---

## 2. Cài đặt môi trường & `.env`

### Bước 1: Khởi tạo môi trường ảo Python
Yêu cầu **Python 3.10+** (khuyến nghị Python 3.12):

```bash
# Tạo môi trường ảo
python -m venv .venv

# Kích hoạt môi trường ảo:
# Trên Windows:
.venv\Scripts\activate
# Trên Linux / macOS:
source .venv/bin/activate
```

### Bước 2: Cài đặt thư viện phụ thuộc
```bash
pip install -r requirements.txt
```

### Bước 3: Cấu hình file `.env`
Sao chép từ `.env.example`:

* **Trên Windows:**
  ```cmd
  copy .env.example .env
  ```
* **Trên Linux / macOS:**
  ```bash
  cp .env.example .env
  ```

Mở file `.env` và điền các giá trị thực tế:

```env
# ==============================================================================
# 1. CẤU HÌNH COREIOT CLOUD (TV3)
# ==============================================================================
COREIOT_URL=https://app.coreiot.io
COREIOT_USERNAME=email_cua_ban@hcmut.edu.vn
COREIOT_PASSWORD=mat_khau_coreiot

COREIOT_HOST=app.coreiot.io
COREIOT_PORT=1883
GATEWAY_NAME=BTL_PC_GATEWAY
DEVICE_NAME=Camera_AI_01
GATEWAY_ACCESS_TOKEN=NHAP_ACCESS_TOKEN_CUA_GATEWAY_TU_COREIOT

# ==============================================================================
# 2. CẤU HÌNH GATEWAY & EDGE AI (TV1 & TV2)
# ==============================================================================
GEMINI_API_KEY=AIzaSy...NHAP_API_KEY_CUA_GEMINI
GEMINI_MODEL=gemini-2.0-flash
YOLO_MODEL_PATH=yolov8n.pt
CAMERA_INDEX=0
CONF_THRESHOLD=0.6
QUARANTINE_FRAMES=15
CAUSAL_WINDOW_SEC=5.0

# ==============================================================================
# 3. CẤU HÌNH LOCAL WEBSERVER & STREAMLIT (TV2)
# ==============================================================================
WEBSERVER_HOST=0.0.0.0
WEBSERVER_PORT=8000
```

> **Lưu ý:** Có thể để trống `GATEWAY_ACCESS_TOKEN` ban đầu. Sau khi tạo Gateway trên CoreIOT (Mục 3), bạn có thể chạy `coreiot_setup.py` để ghi tự động vào `.env` hoặc nhập trực tiếp trên trang cấu hình Webserver `http://localhost:8000` (hệ thống sẽ tự lưu và nạp lại nóng không cần restart).

---

## 3. Thiết lập CoreIOT Cloud (TV3 – T15, T11, T12, T13)

Có 2 phương thức thiết lập nền tảng CoreIOT Cloud:

### Cách A: Tự động hoàn toàn bằng Script (Khuyên dùng khi triển khai nhanh)
Chạy script REST API để tự động khởi tạo Gateway, Rule Chain, Notification và Dashboard:

```bash
# 1. Chạy thiết lập toàn bộ và tự ghi token vào .env
python tools/coreiot_setup.py --env .env

# 2. Sau khi Gateway kết nối lần đầu (tự sinh Camera_AI_01), gán profile và khung giờ cấm:
python tools/coreiot_setup.py --assign-only
# Tuỳ chọn: đặt khung giờ cấm thử nghiệm ban ngày từ 8h -> 17h:
python tools/coreiot_setup.py --assign-only --forbidden 8 17
```

---

### Cách B: Thực hiện thủ công trên giao diện Web CoreIOT (Dùng để chụp ảnh báo cáo)

#### Bước 3.1: Tạo Gateway (T15)
1. Đăng nhập [app.coreiot.io](https://app.coreiot.io) → vào menu **Entities** → **Devices** → nhấn `+` (Add new device).
2. Tên thiết bị: `BTL_PC_GATEWAY`.
3. ✅ **Tích chọn "Is gateway"** *(Bắt buộc: nếu thiếu, Broker sẽ không nhận dạng Gateway API và không tự động sinh thiết bị con)*.
4. Bỏ tích *Overwrite activity time for connected device* → Nhấn **Add**.
5. Mở thiết bị `BTL_PC_GATEWAY` vừa tạo → tab **Details** → nhấn **Copy access token** (hoặc xem tại *Manage credentials*) → dán vào `.env` (`GATEWAY_ACCESS_TOKEN=`).

#### Bước 3.2: Cấu hình Rule Chain lọc khung giờ cấm & báo động (T12)
1. Mở menu **Rule chains** → nhấn `+` → chọn **Import rule chain**.
2. Chọn file [rulechain_btl_camera_ai_security.json](file:///c:/Users/quang/Downloads/filebaocao/iot/BTL_IoT_complete/BTL_IoT_Camera_AI/coreiot/rulechain_btl_camera_ai_security.json) trong thư mục `coreiot/` → Nhấn **Import**.
3. Mở Rule Chain vừa import → nhấn **Apply changes** (biểu tượng tích ✔ ở góc dưới cùng bên phải).
4. **Giải thích các node chính trong RuleChain:**
   - **Node 1 (message type switch):** Phân luồng bản tin. `Post telemetry` rẽ vào Node 2; `RPC Request to Device` rẽ vào Node 4.
   - **Node 2 (save timeseries):** Lưu trữ dữ liệu viễn trắc (`people_count`, `anomaly_detected`, `llm_alert`).
   - **Node 4 (rpc call request):** Chuyển tiếp lệnh RPC xuống Gateway (tránh lỗi RPC Switch timeout).
   - **Node 5 (originator attributes):** Bổ sung thuộc tính khung giờ cấm (`forbidden_start_hour`, `forbidden_end_hour`) từ Server Scope vào metadata.
   - **Node 6 (script filter):** Kiểm tra điều kiện `people_count > 0` trong khung giờ cấm (mặc định 22h đêm đến 6h sáng hôm sau theo giờ Việt Nam GMT+7). Nếu thỏa mãn → nhánh `True` sang Node 7; nếu không → nhánh `False` sang Node 8 (`clear alarm`).
   - **Node 7 (create alarm):** Tạo Alarm mức **Critical**, loại `Xâm nhập ngoài giờ`. Cập nhật `max_people`, thời gian và câu cảnh báo của LLM.
   - **Node 9 (script transformation):** Trích xuất biến `${people_count}`, `${hour_vn}`, `${llm_alert}` chuẩn bị cho thông báo.

#### Bước 3.3: Tạo Device Profile `Camera_AI`
1. Mở menu **Profiles** → **Device profiles** → nhấn `+` → **Create new device profile**.
2. Đặt tên: `Camera_AI`.
3. Mục **Default rule chain**: Chọn `BTL Camera AI Security`.
4. Nhấn **Add**.
5. *(Lưu ý: Do `mqtt_gateway.py` đã gửi kèm `"type": "Camera_AI"` khi connect, thiết bị con `Camera_AI_01` khi tự động tạo sẽ được gắn thẳng vào profile này).*

#### Bước 3.4: Cấu hình Notification Center gửi chuông đỏ (T13)
1. **Tạo người nhận (Recipients):** Mở **Notification center** → **Recipients** → `+ Add recipients`:
   - Name: `BTL – Tenant administrators`
   - Type: `Platform users`
   - User filter: `Tenant administrators` → **Add**.
2. **Tạo bản mẫu thông báo (Template):** Mở **Templates** → `+ Add template`:
   - Name: `BTL – Cảnh báo xâm nhập ngoài giờ`
   - Type: `Rule node`
   - Delivery method: ✅ Chọn **Web**
   - Subject: `🚨 Xâm nhập ngoài giờ – ${deviceName}`
   - Message: `Phát hiện ${people_count} người lúc ${hour_vn}h (khung giờ cấm). AI: ${llm_alert}`
   - Icon: `notifications_active`, màu `#D12730` → **Add**.
3. **Nối vào Rule Chain:** Mở lại Rule Chain `BTL Camera AI Security`, kéo node **send notification** vào cuối luồng, liên kết với Template và Recipients vừa tạo, nối từ nhánh `Success` của Node 9 sang Node Notification → **Apply changes**.

#### Bước 3.5: Cấu hình Dashboard CoreIOT (T11)
1. Mở menu **Dashboards** → nhấn `+` → **Import dashboard**.
2. Chọn file [dashboard_btl_camera_ai.json](file:///c:/Users/quang/Downloads/filebaocao/iot/BTL_IoT_complete/BTL_IoT_Camera_AI/coreiot/dashboard_btl_camera_ai.json) trong thư mục `coreiot/` → Nhấn **Import**.
3. Dashboard cung cấp đầy đủ các thành phần:
   - **Thẻ Số người hiện tại:** Hiển thị `people_count` thời gian thực.
   - **Thẻ Trạng thái an ninh:** Hiển thị cảnh báo khi `anomaly_detected = true`.
   - **Đồ thị lượng người:** Time series chart theo dõi biến động số người trong 30 phút qua.
   - **Công tắc an ninh AI (RPC Switch):** Gửi RPC `set_ai_status` để bật/tắt module AI từ xa và đọc trạng thái bằng `get_ai_status`.
   - **Bảng Cảnh báo (Alarms Table):** Quản lý trạng thái các báo động `Xâm nhập ngoài giờ`.
   - **Nhật ký cảnh báo ngữ nghĩa LLM:** Bảng timeseries lưu lại chi tiết câu cảnh báo tự nhiên từ Gemini.

---

## 4. Khởi chạy hệ thống Local (TV1 + TV2)

### 4.1 Khởi chạy đồng bộ bằng 1 lệnh (`run_all.py`)
Script `run_all.py` sẽ khởi động song song 2 tiến trình:
1. **IoT Gateway Backend (`gateway_main.py`):** Chạy Edge AI (TV1), MQTT Gateway Bridge T08 và Local Webserver FastAPI T07.
2. **Streamlit Monitoring Dashboard (`streamlit_app.py`):** Giao diện theo dõi cục bộ T10.

* **Chế độ chạy thực tế (có Webcam & YOLOv8 + Gemini):**
  ```bash
  python run_all.py
  ```
* **Chế độ chạy Mock giả lập (không cần Webcam, phục vụ test kết nối Cloud & UI):**
  ```bash
  python run_all.py --mock
  ```

---

### 4.2 Khởi chạy riêng lẻ từng tiến trình (Dành cho việc gỡ lỗi)
Mở 2 cửa sổ terminal riêng biệt:

```bash
# Terminal 1: Chạy Backend Gateway & Edge AI
python gateway_main.py

# Terminal 2: Chạy giao diện Streamlit
streamlit run streamlit_app.py --server.port=8501
```

---

### 4.3 Các cổng dịch vụ & địa chỉ truy cập

| Dịch vụ | Địa chỉ URL | Mô tả chức năng |
| :--- | :--- | :--- |
| **Streamlit Dashboard (T10)** | `http://localhost:8501` | Giám sát trạng thái AI, đồ thị telemetry, nhật ký RPC và Audit Log. |
| **Local Webserver (T07)** | `http://localhost:8000` | Trang trạng thái tổng quan và Form cấu hình động (hot-reload không cần tắt app). |
| **Video Stream MJPEG** | `http://localhost:8000/video_feed` | Luồng hình ảnh camera trực tiếp có vẽ bounding box YOLO cho các máy trong mạng LAN. |
| **Gateway REST API** | `http://localhost:8000/api/status` | API JSON báo cáo tình trạng kết nối MQTT, số người và trạng thái AI. |

---

## 5. Chạy kiểm thử tự động (Unit & Integration Test)

Dự án tích hợp đầy đủ bộ kiểm thử tự động cho toàn bộ hệ thống, chạy offline không cần Internet hay phần cứng webcam.

### 5.1 Lệnh chạy toàn bộ kiểm thử
Từ thư mục gốc dự án, thực hiện lệnh:

```bash
python -m unittest discover -s tests -v
```

### 5.2 Nội dung các bài kiểm thử trong `tests/`

#### 1. `test_gateway.py` (Kiểm thử TV2 & Tích hợp Edge AI):
- `test_telemetry_contract_format`: Kiểm tra cấu trúc đóng gói gói tin `v1/gateway/telemetry` chuẩn xác theo hợp đồng JSON Gateway API `{"Camera_AI_01": [{"ts": ..., "values": {...}}]}`.
- `test_connect_payload_device_type`: Kiểm chứng bản vá bản tin `v1/gateway/connect` luôn gửi kèm `"type": "Camera_AI"` để CoreIOT nhận diện Device Profile.
- `test_rpc_set_and_get_ai_status`: Giả lập lệnh RPC từ CoreIOT Dashboard gửi xuống (`set_ai_status`, `get_ai_status`) và xác thực Gateway phản hồi boolean đúng topic `v1/gateway/rpc/response/{id}` và `v1/gateway/rpc`.
- `test_webserver_endpoints_and_config_reload`: Kiểm tra các API của Local Webserver (FastAPI), chức năng toggle AI `/api/ai/toggle` và tính năng nạp lại cấu hình động `/config`.

#### 2. `test_tv3.py` (Kiểm thử CoreIOT Cloud & Automation):
- `TestRuleChainScripts`: Kiểm tra logic JavaScript lọc khung giờ cấm, xử lý qua nửa đêm (22h -> 6h), tích lũy báo động và chuyển đổi biến template thông báo (yêu cầu Node.js, tự động bỏ qua nếu máy chưa cài Node).
- `TestJsonTemplates`: Xác thực cấu trúc file JSON Rule Chain (`rulechain_btl_camera_ai_security.json`) và Dashboard (`dashboard_btl_camera_ai.json`), đảm bảo đủ node RPC Request để tránh lỗi timeout.
- `TestSetupScript`: Khởi tạo một HTTP server giả lập toàn bộ REST API của CoreIOT, kiểm thử luồng chạy của `coreiot_setup.py` (đăng nhập, tạo Gateway, cấp token, cấu hình rulechain, gán profile và gán khung giờ cấm).

---

## 6. Kịch bản kiểm thử End-to-End khi báo cáo & Demo

| Bước | Thao tác thực hiện | Kết quả quan sát thực tế | Thành phần kiểm chứng |
| :---: | :--- | :--- | :---: |
| **1** | Chạy `python run_all.py` (với Token trong `.env`) | Console báo kết nối CoreIOT thành công. Trên CoreIOT mục Devices xuất hiện `Camera_AI_01` (Active, profile `Camera_AI`). | T15 (Đồng bộ Gateway) |
| **2** | Đứng trước Webcam ~2 giây (vượt qua 15 frame Quarantine) | Sau đúng chu kỳ 5s Causal Window: Streamlit UI và CoreIOT Dashboard cập nhật `people_count = 1`, `anomaly_detected = true`, bảng LLM hiện câu cảnh báo tiếng Việt từ Gemini. | TV1 + TV2 + T11 (Telemetry) |
| **3** | Gạt **Công tắc an ninh AI** sang **OFF** trên CoreIOT Dashboard | Công tắc chuyển trạng thái tức thì không xoay vòng/timeout. Streamlit UI chuyển sang trạng thái "Tắt AI", webcam không tốn tài nguyên gọi Gemini, `people_count` trả về `0`. | T11 + T08 (RPC Điều khiển) |
| **4** | Gạt lại sang **ON** | AI kích hoạt trở lại. Làm mới trang Dashboard CoreIOT vẫn giữ đúng trạng thái ON (nhờ RPC `get_ai_status`). | T11 + T08 (RPC Phản hồi) |
| **5** | Đặt khung giờ cấm trùng giờ hiện tại (vd: `python tools/coreiot_setup.py --assign-only --forbidden 8 17`) và xuất hiện trước camera | Xuất hiện Alarm mức **Critical** loại `Xâm nhập ngoài giờ`. Quả chuông 🔔 góc trên CoreIOT báo đỏ và hiển thị chi tiết câu văn Gemini sinh ra. | T12 + T13 (Rule Chain & Alert) |
| **6** | Rời khỏi vùng quan sát của Webcam | Sau 5s, Alarm trên CoreIOT tự động chuyển sang trạng thái **Cleared**. | T12 (Tự động xóa cảnh báo) |

> **Mẹo demo khi không có thiết bị/webcam:** Sử dụng công cụ giả lập `gateway_simulator.py`:
> ```bash
> # Giả lập 2 người lúc 23h đêm (rơi vào khung giờ cấm) -> Kích hoạt Alarm & Chuông đỏ
> python tools/gateway_simulator.py --token <ACCESS_TOKEN> --people 2 --fake-hour 23 --count 3
>
> # Giả lập 0 người -> Tự động Clear Alarm
> python tools/gateway_simulator.py --token <ACCESS_TOKEN> --people 0 --fake-hour 23 --count 1
> ```

---

## 7. Xử lý sự cố thường gặp (Troubleshooting)

| Hiện tượng lỗi | Nguyên nhân gốc rễ | Hướng dẫn khắc phục |
| :--- | :--- | :--- |
| **CoreIOT từ chối kết nối (rc=4 hoặc rc=5)** | Sai `GATEWAY_ACCESS_TOKEN` hoặc chưa tạo Gateway trên Cloud. | Kiểm tra lại Access Token của thiết bị `BTL_PC_GATEWAY` trên CoreIOT và cập nhật vào `.env` hoặc tại `http://localhost:8000`. |
| **Không thấy `Camera_AI_01` tự sinh trên CoreIOT** | Gateway chưa được bật cờ `Is gateway`. | Mở thiết bị `BTL_PC_GATEWAY` trên CoreIOT, tích chọn **Is gateway**, lưu lại và khởi động lại `gateway_main.py`. |
| **Camera báo lỗi "Waiting for Video Frame"** | Webcam bị phần mềm khác chiếm dụng hoặc sai `CAMERA_INDEX`. | Tắt các ứng dụng đang dùng webcam (Zoom, Teams, OBS) hoặc thử đổi `CAMERA_INDEX=1` trong `.env`. |
| **Công tắc Switch Widget trên Cloud báo Timeout** | Switch gửi RPC trực tiếp tới Gateway thay vì `Camera_AI_01`, hoặc thiếu node `RPC Request to Device`. | Trong cấu hình Switch Widget, chọn Target Device là alias `Camera_AI_01`. Đảm bảo Rule Chain có node `rpc call request` nối từ nhánh `RPC Request to Device`. |
| **Có Alarm nhưng không có thông báo chuông đỏ 🔔** | Chưa nối node `send notification` hoặc người nhận không chứa tài khoản đang đăng nhập. | Kiểm tra Notification Center: đảm bảo Recipients là `Tenant administrators` và template được liên kết chính xác trong Rule Chain. |
| **Xung đột cổng `8000` hoặc `8501`** | Tiến trình cũ chưa dừng hoàn toàn. | Đổi cổng `WEBSERVER_PORT=8080` trong `.env` hoặc tắt tiến trình Python cũ trong Task Manager. |
