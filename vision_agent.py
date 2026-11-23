"""
vision_agent.py – Module phân tích hình ảnh Edge AI cho IoT Gateway
====================================================================

Tác giả  : Edge AI Engineer
Phiên bản: 1.0.0
Mô tả    : Sử dụng YOLOv8n (ultralytics) + OpenCV để đếm người qua webcam,
            kết hợp Google Generative AI (gemini) sinh cảnh báo ngôn ngữ tự nhiên.

Cơ chế chống nhiễu:
  • Quarantine Isolation – Bounding‑box chỉ được xác nhận khi confidence > 0.6
    duy trì liên tục >= 15 frames (tránh false‑positive chớp nhoáng).
  • Causal Windowing   – Kết quả chỉ được tổng hợp & yield mỗi 5 giây,
    không trả về liên tục từng frame.

Sử dụng:
    >>> from vision_agent import VisionAgent
    >>> agent = VisionAgent(gemini_api_key="YOUR_KEY")
    >>> for payload in agent.run_inference_loop():
    ...     print(payload)  # dict theo AIPayloadSchema
"""

from __future__ import annotations

import logging
import os
import time
import threading
from collections import defaultdict
from typing import Generator

import cv2
import numpy as np
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("VisionAgent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(name)s | %(levelname)s | %(message)s")
    )
    logger.addHandler(_handler)


# ═══════════════════════════════════════════════════════════════════════════
# 1. DATA CONTRACT – Pydantic v2
# ═══════════════════════════════════════════════════════════════════════════
class AIPayloadSchema(BaseModel):
    """Hợp đồng dữ liệu (Data Contract) cho kết quả phân tích hình ảnh.

    Attributes:
        people_count:     Số lượng người đã qua bộ lọc Quarantine.
        anomaly_detected: True nếu phát hiện ≥ 1 người.
        llm_alert:        Thông điệp cảnh báo ngôn ngữ tự nhiên từ LLM
                          (chuỗi rỗng nếu không có người).
    """

    people_count: int = Field(
        default=0,
        description="Số lượng người đếm được trong khung hình",
    )
    anomaly_detected: bool = Field(
        default=False,
        description="True nếu có người",
    )
    llm_alert: str = Field(
        default="",
        description="Thông điệp cảnh báo từ LLM",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 2. QUARANTINE TRACKER – Bộ lọc chống nhiễu cấp bounding‑box
# ═══════════════════════════════════════════════════════════════════════════
class _QuarantineTracker:
    """Theo dõi mỗi bounding‑box theo track_id.

    Một track_id chỉ được coi là **xác nhận** (confirmed) khi nó duy trì
    confidence > ``conf_threshold`` liên tục trong ít nhất
    ``min_consecutive_frames`` frames.

    Nếu tại bất kỳ frame nào confidence ≤ ngưỡng hoặc track_id biến mất,
    bộ đếm liên tục sẽ bị reset về 0.
    """

    def __init__(
        self,
        conf_threshold: float = 0.6,
        min_consecutive_frames: int = 15,
    ) -> None:
        self._conf_threshold = conf_threshold
        self._min_frames = min_consecutive_frames

        # track_id → số frame liên tục vượt ngưỡng
        self._streak: dict[int, int] = defaultdict(int)
        # Tập track_id đã xuất hiện ở frame trước (để phát hiện biến mất)
        self._prev_ids: set[int] = set()

    def update(
        self,
        detections: list[tuple[int, float]],
    ) -> int:
        """Cập nhật tracker với danh sách (track_id, confidence) của frame hiện tại.

        Args:
            detections: Danh sách các cặp ``(track_id, confidence)`` –
                        chỉ bao gồm class ``person``.

        Returns:
            Số lượng người **đã xác nhận** (streak ≥ min_consecutive_frames).
        """
        current_ids: set[int] = set()

        for track_id, conf in detections:
            current_ids.add(track_id)
            if conf > self._conf_threshold:
                self._streak[track_id] += 1
            else:
                # Confidence tụt → reset streak
                self._streak[track_id] = 0

        # Track_id biến mất khỏi frame → reset streak
        disappeared = self._prev_ids - current_ids
        for tid in disappeared:
            self._streak.pop(tid, None)

        self._prev_ids = current_ids

        # Đếm số track_id đã qua quarantine
        confirmed = sum(
            1
            for tid in current_ids
            if self._streak.get(tid, 0) >= self._min_frames
        )
        return confirmed

    def reset(self) -> None:
        """Xoá toàn bộ trạng thái (khi tạm dừng / khởi động lại)."""
        self._streak.clear()
        self._prev_ids.clear()


# ═══════════════════════════════════════════════════════════════════════════
# 3. LLM CALLER – Gọi Google Generative AI (Gemini) sinh cảnh báo
# ═══════════════════════════════════════════════════════════════════════════
class _LLMCaller:
    """Đóng gói logic gọi LLM API (Google Generative AI – Gemini).

    Chỉ gọi khi ``people_count > 0`` để tiết kiệm quota & chi phí.
    Nếu API key không được cung cấp hoặc gọi thất bại, trả về
    thông điệp mặc định (fallback) thay vì crash.
    """

    # Prompt hệ thống hướng dẫn LLM sinh cảnh báo ngắn gọn
    _SYSTEM_PROMPT = (
        "Bạn là hệ thống cảnh báo an ninh IoT. "
        "Khi nhận thông tin về số lượng người phát hiện được từ camera, "
        "hãy trả về MỘT câu cảnh báo ngắn gọn bằng tiếng Việt "
        "(tối đa 2 câu, không giải thích thêm)."
    )

    def __init__(self, api_key: str | None = None, model_name: str = "gemini-2.0-flash") -> None:
        self._api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._model_name = model_name
        self._client = None  # lazy init

    def _ensure_client(self) -> bool:
        """Khởi tạo client lần đầu. Trả về True nếu sẵn sàng."""
        if self._client is not None:
            return True
        if not self._api_key:
            logger.warning("GEMINI_API_KEY chưa được cấu hình – LLM sẽ trả fallback.")
            return False
        try:
            from google import genai  # type: ignore[import-untyped]

            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"timeout": 8000},
                )
            logger.info("Kết nối Google GenAI thành công (model=%s).", self._model_name)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Không thể khởi tạo Google GenAI client: %s", exc)
            return False

    def generate_alert(self, people_count: int) -> str:
        """Sinh cảnh báo từ LLM dựa trên số người phát hiện.

        Args:
            people_count: Số người đã qua bộ lọc Quarantine.

        Returns:
            Chuỗi cảnh báo ngắn gọn, hoặc chuỗi rỗng nếu ``people_count == 0``.
        """
        if people_count <= 0:
            return ""

        user_msg = (
            f"Camera phát hiện {people_count} người trong khu vực giám sát. "
            f"Hãy đưa ra cảnh báo."
        )

        # Thử gọi LLM
        if self._ensure_client():
            try:
                response = self._client.models.generate_content(
                    model=self._model_name,
                    contents=[
                        {"role": "user", "parts": [{"text": self._SYSTEM_PROMPT}]},
                        {"role": "user", "parts": [{"text": user_msg}]},
                    ],
                )
                text = response.text.strip() if response.text else ""
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001
                logger.error("Lỗi khi gọi Gemini API: %s", exc)

        # Fallback – đảm bảo luôn có output hợp lệ
        return f"[CẢNH BÁO] Phát hiện {people_count} người trong vùng giám sát!"


# ═══════════════════════════════════════════════════════════════════════════
# 4. VISION AGENT – Module chính
# ═══════════════════════════════════════════════════════════════════════════
class VisionAgent:
    """Module phân tích hình ảnh Edge AI cho IoT Gateway.

    Tích hợp:
      • **YOLOv8n** (ultralytics) – phát hiện & tracking người (class ``person``).
      • **Quarantine Isolation** – lọc false‑positive (confidence > 0.6 × 15 frames).
      • **Causal Windowing** – tổng hợp & yield kết quả mỗi 5 giây.
      • **Google GenAI (Gemini)** – sinh cảnh báo ngôn ngữ tự nhiên.

    Ví dụ sử dụng::

        agent = VisionAgent(
            gemini_api_key="AIza...",
            camera_index=0,
            causal_window_sec=5.0,
        )

        for payload in agent.run_inference_loop():
            # payload: dict theo AIPayloadSchema
            mqtt_client.publish("iot/vision", json.dumps(payload))

    Để tạm dừng / tiếp tục từ bên ngoài (vd: qua MQTT command)::

        agent.set_ai_status(False)   # Tạm dừng quét
        agent.set_ai_status(True)    # Tiếp tục quét
    """

    # YOLO class ID cho "person" trong COCO dataset
    _PERSON_CLASS_ID: int = 0

    def __init__(
        self,
        gemini_api_key: str | None = None,
        gemini_model: str = "gemini-2.0-flash",
        yolo_model_path: str = "yolov8n.pt",
        camera_index: int = 0,
        conf_threshold: float = 0.6,
        quarantine_frames: int = 15,
        causal_window_sec: float = 5.0,
    ) -> None:
        """Khởi tạo VisionAgent.

        Args:
            gemini_api_key:     API key của Google Generative AI (Gemini).
                                Nếu None, đọc từ env var ``GEMINI_API_KEY``.
            gemini_model:       Tên model Gemini (mặc định ``gemini-2.0-flash``).
            yolo_model_path:    Đường dẫn / tên model YOLO (mặc định ``yolov8n.pt``).
            camera_index:       Chỉ số webcam (0 = mặc định).
            conf_threshold:     Ngưỡng confidence tối thiểu cho Quarantine.
            quarantine_frames:  Số frame liên tục cần duy trì để xác nhận.
            causal_window_sec:  Chu kỳ Causal Window (giây).
        """
        # --- Cấu hình ---
        self._camera_index = camera_index
        self._causal_window_sec = causal_window_sec
        self._yolo_model_path = yolo_model_path

        # --- Trạng thái on/off (thread‑safe) ---
        self._active = True
        self._lock = threading.Lock()

        # --- Bộ lọc Quarantine ---
        self._quarantine = _QuarantineTracker(
            conf_threshold=conf_threshold,
            min_consecutive_frames=quarantine_frames,
        )

        # --- LLM Caller ---
        self._llm = _LLMCaller(api_key=gemini_api_key, model_name=gemini_model)

        # --- Frame mới nhất cho Webserver T07 (thread‑safe) ---
        self._latest_frame: np.ndarray | None = None

        # --- YOLO model (lazy load) ---
        self._model = None

        logger.info(
            "VisionAgent khởi tạo: camera=%d | quarantine=%d frames | "
            "causal_window=%.1fs | yolo=%s",
            camera_index,
            quarantine_frames,
            causal_window_sec,
            yolo_model_path,
        )

    # ── Public API ──────────────────────────────────────────────────────

    def set_ai_status(self, status: bool) -> None:
        """Bật / tắt quá trình quét webcam từ module bên ngoài.

        Khi ``status=False``, vòng lặp inference sẽ tạm dừng xử lý frame
        (webcam vẫn mở nhưng không chạy detection) và yield payload trống.
        Khi ``status=True``, quá trình quét được khôi phục.

        Args:
            status: ``True`` để kích hoạt, ``False`` để tạm dừng.
        """
        with self._lock:
            self._active = status
            if not status:
                # Reset tracker khi tạm dừng để tránh dữ liệu cũ
                self._quarantine.reset()
        logger.info("AI status → %s", "ACTIVE" if status else "PAUSED")

    @property
    def is_active(self) -> bool:
        """Trả về trạng thái hiện tại (thread‑safe)."""
        with self._lock:
            return self._active

    def get_ai_status(self) -> bool:
        """Trả về trạng thái AI hiện tại – đồng bộ với RPC 'get_ai_status' từ CoreIOT Gateway.

        Thành viên 2 (Gateway Engineer) gọi method này để đồng bộ trạng thái
        nút gạt trên giao diện CoreIOT khi khởi động.

        Returns:
            ``True`` nếu AI đang quét, ``False`` nếu đang tạm dừng.
        """
        return self.is_active

    def get_latest_frame(self) -> np.ndarray | None:
        """Trả về frame mới nhất (đã đọc từ webcam) cho Local Webserver T07.

        Vì ``cv2.VideoCapture`` chỉ cho phép một tiến trình giữ webcam,
        method này cho phép Thành viên 2 lấy frame ra để stream lên
        Flask/FastAPI mà không cần mở thêm kết nối camera.

        Returns:
            Bản sao của frame BGR (numpy array), hoặc ``None`` nếu chưa có frame.
        """
        with self._lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def run_inference_loop(self) -> Generator[dict, None, None]:
        """Generator chính – mở webcam, chạy detection & yield kết quả.

        Quy trình mỗi vòng lặp:
          1. Đọc frame từ webcam.
          2. Lưu frame mới nhất vào ``_latest_frame`` (cho Webserver T07).
          3. Chạy YOLOv8 tracking (BoT‑SORT) trên frame.
          4. Trích xuất danh sách (track_id, confidence) của class ``person``.
          5. Cập nhật Quarantine Tracker → nhận ``confirmed_count``.
          6. Ghi nhận giá trị cực đại ``max_confirmed_in_window`` trong cửa sổ.
          7. Sau mỗi ``causal_window_sec`` giây:
             a. Lấy ``max_confirmed_in_window`` (đỉnh cả cửa sổ, không chỉ frame cuối).
             b. Nếu ``max_confirmed_in_window > 0`` → gọi LLM sinh cảnh báo.
             c. ``yield`` dict từ ``AIPayloadSchema.model_dump()``.
             d. Reset ``max_confirmed_in_window = 0`` cho cửa sổ tiếp theo.

        Yields:
            dict – Kết quả theo schema ``AIPayloadSchema``.

        Raises:
            RuntimeError: Nếu không mở được webcam.
        """
        # ----- Lazy‑load YOLO model -----
        if self._model is None:
            self._model = self._load_yolo_model()

        # ----- Mở webcam -----
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            raise RuntimeError(
                f"Không thể mở webcam (camera_index={self._camera_index}). "
                "Kiểm tra kết nối thiết bị."
            )

        logger.info("Webcam đã mở (index=%d). Bắt đầu inference loop…", self._camera_index)

        try:
            window_start: float = time.monotonic()
            max_confirmed_in_window: int = 0  # Đỉnh số người xác nhận trong cửa sổ 5s

            while True:
                ret, frame = cap.read()
                if not ret:
                    logger.warning("Không đọc được frame – thử lại…")
                    time.sleep(0.05)
                    continue

                # Lưu frame mới nhất cho Webserver T07 (thread‑safe)
                with self._lock:
                    self._latest_frame = frame

                # ── Kiểm tra trạng thái active ──
                if not self.is_active:
                    # Tạm dừng: vẫn đọc frame (giữ webcam sống) nhưng không xử lý
                    elapsed = time.monotonic() - window_start
                    if elapsed >= self._causal_window_sec:
                        # Yield payload trống khi đang PAUSED
                        logger.debug("PAUSED – yield payload trống.")
                        yield AIPayloadSchema().model_dump()
                        window_start = time.monotonic()
                        max_confirmed_in_window = 0

                    # ----------------------------------------------------
                    # [THÊM MỚI] Hiển thị cửa sổ khi AI đang PAUSED
                    # ----------------------------------------------------
                    # cv2.imshow("Vision Agent Camera", frame)
                    # if cv2.waitKey(1) & 0xFF == ord('q'):
                    #     break
                    # ----------------------------------------------------

                    time.sleep(0.03)
                    continue

                # ── Chạy YOLOv8 tracking ──
                person_detections = self._detect_persons(frame)

                # --------------------------------------------------------
                # [THÊM MỚI] Vẽ bounding box lên hình ảnh để xem kết quả YOLO
                # --------------------------------------------------------
                # annotated_frame = frame.copy()
                # # Cập nhật vòng lặp lấy thêm biến box
                # for track_id, conf, box in person_detections:
                #     x1, y1, x2, y2 = box
                    
                #     # Vẽ bounding box màu xanh lá (hoặc đỏ tuỳ bạn đổi mã BGR)
                #     cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    
                #     # Hiển thị ID và Conf ngay trên đầu khung box
                #     label = f"ID:{track_id} Conf:{conf:.2f}"
                #     cv2.putText(annotated_frame, label, (x1, y1 - 10), 
                #                 cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                
                # cv2.imshow("Vision Agent Camera", annotated_frame)
                # if cv2.waitKey(1) & 0xFF == ord('q'):
                #     break
                # --------------------------------------------------------

                # ── Cập nhật Quarantine Tracker ──
                #confirmed = self._quarantine.update(person_detections)
                tracker_input = [(tid, conf) for tid, conf, box in person_detections]
                confirmed = self._quarantine.update(tracker_input)

                # Ghi nhận giá trị cực đại trong suốt cửa sổ
                if confirmed > max_confirmed_in_window:
                    max_confirmed_in_window = confirmed

                # ── Causal Window check ──
                elapsed = time.monotonic() - window_start
                if elapsed >= self._causal_window_sec:
                    # Tổng hợp kết quả (dùng đỉnh cả cửa sổ) & gọi LLM nếu cần
                    payload = self._build_payload(max_confirmed_in_window)
                    logger.info(
                        "⏱ Causal Window kết thúc: people=%d | anomaly=%s | alert='%s'",
                        payload.people_count,
                        payload.anomaly_detected,
                        payload.llm_alert[:60] + "…" if len(payload.llm_alert) > 60 else payload.llm_alert,
                    )
                    yield payload.model_dump()

                    # Reset cho cửa sổ 5s tiếp theo
                    window_start = time.monotonic()
                    max_confirmed_in_window = 0

                # Giảm CPU usage – phù hợp edge device
                time.sleep(0.01)

        except GeneratorExit:
            logger.info("Inference loop được dừng bởi caller.")
        finally:
            cap.release()
            logger.info("Webcam đã giải phóng.")

    # ── Private helpers ─────────────────────────────────────────────────

    def _load_yolo_model(self):
        """Lazy‑load model YOLOv8 từ ultralytics.

        Returns:
            ultralytics.YOLO instance.
        """
        try:
            from ultralytics import YOLO  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "Thư viện 'ultralytics' chưa được cài đặt. "
                "Chạy: pip install ultralytics"
            ) from exc

        logger.info("Đang tải model YOLO: %s …", self._yolo_model_path)
        model = YOLO(self._yolo_model_path)
        logger.info("Model YOLO đã sẵn sàng.")
        return model

    def _detect_persons(
        self,
        frame: np.ndarray,
    ) -> list[tuple[int, float]]:
        """Chạy YOLOv8 tracking trên frame và trả về danh sách person detections.

        Sử dụng built‑in tracker (BoT‑SORT) của ultralytics để gán track_id
        ổn định cho mỗi bounding‑box qua các frame.

        Args:
            frame: Ảnh BGR từ webcam (numpy array).

        Returns:
            Danh sách ``(track_id, confidence)`` chỉ cho class ``person``.
        """
        # persist=True: giữ tracker state liên tục giữa các frame

        
        # results = self._model.track(
        #     source=frame,
        #     persist=True,
        #     classes=[self._PERSON_CLASS_ID],  # Chỉ detect class "person"
        #     verbose=False,  # Tắt log mỗi frame
        # )

        # detections: list[tuple[int, float]] = []

        # for result in results:
        #     boxes = result.boxes
        #     if boxes is None or boxes.id is None:
        #         continue

        #     track_ids = boxes.id.cpu().numpy().astype(int)
        #     confidences = boxes.conf.cpu().numpy().astype(float)
        #     class_ids = boxes.cls.cpu().numpy().astype(int)

        #     for tid, conf, cls_id in zip(track_ids, confidences, class_ids):
        #         if cls_id == self._PERSON_CLASS_ID:
        #             detections.append((int(tid), float(conf)))


        results = self._model.track(
            source=frame,
            persist=True,
            classes=[self._PERSON_CLASS_ID],
            verbose=False,
        )

        detections: list[tuple[int, float, list[int]]] = [] # Cập nhật type hint

        for result in results:
            boxes = result.boxes
            if boxes is None or boxes.id is None:
                continue

            track_ids = boxes.id.cpu().numpy().astype(int)
            confidences = boxes.conf.cpu().numpy().astype(float)
            class_ids = boxes.cls.cpu().numpy().astype(int)
            # Lấy toạ độ bounding box (x1, y1, x2, y2)
            xyxy_boxes = boxes.xyxy.cpu().numpy().astype(int)

            for tid, conf, cls_id, box in zip(track_ids, confidences, class_ids, xyxy_boxes):
                if cls_id == self._PERSON_CLASS_ID:
                    detections.append((int(tid), float(conf), box.tolist()))

        return detections

    def _build_payload(self, confirmed_count: int) -> AIPayloadSchema:
        """Xây dựng payload cuối cùng cho một Causal Window.

        Nếu có người xác nhận, gọi LLM để sinh cảnh báo.
        Nếu không, trả về payload mặc định (trống).

        Args:
            confirmed_count: Số người đã qua bộ lọc Quarantine.

        Returns:
            ``AIPayloadSchema`` instance.
        """
        if confirmed_count <= 0:
            return AIPayloadSchema(
                people_count=0,
                anomaly_detected=False,
                llm_alert="",
            )

        # Gọi LLM sinh cảnh báo
        alert_text = self._llm.generate_alert(confirmed_count)

        return AIPayloadSchema(
            people_count=confirmed_count,
            anomaly_detected=True,
            llm_alert=alert_text,
        )


# ═══════════════════════════════════════════════════════════════════════════
# 5. STANDALONE ENTRY POINT – Chạy thử trực tiếp
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import json

    print("=" * 60)
    print("  VisionAgent – Edge AI Module Demo")
    print("=" * 60)

    # Đọc API key từ biến môi trường hoặc truyền trực tiếp
    api_key = os.getenv("GEMINI_API_KEY", None)

    agent = VisionAgent(
        gemini_api_key=api_key,
        camera_index=0,
        conf_threshold=0.6,
        quarantine_frames=15,
        causal_window_sec=5.0,
    )

    try:
        for payload in agent.run_inference_loop():
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            print("-" * 40)
    except KeyboardInterrupt:
        print("\n[INFO] Dừng bởi người dùng (Ctrl+C).")
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")

