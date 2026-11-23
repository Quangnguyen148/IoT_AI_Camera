"""
coreiot_setup.py – Tự động cấu hình CoreIOT cho phần việc của Thành viên 3
===========================================================================
Thực hiện qua REST API của CoreIOT (nền ThingsBoard):

  T15  Tạo thiết bị Gateway "BTL_PC_GATEWAY" (bật cờ Is gateway), lấy Access Token,
       ghi token vào file .env của TV2 (tuỳ chọn --tv2-env).
  T12  Tạo/ cập nhật Rule Chain "BTL Camera AI Security" (lọc khung giờ cấm -> Create/Clear Alarm).
  T13  Tạo Notification target + template, nối node "Send Notification" vào cuối Rule Chain.
       Tạo Device profile "Camera_AI" dùng Rule Chain trên; chuyển Camera_AI_01 sang profile này.
  T11  Import Dashboard "BTL IoT – Giám sát Camera AI".

Cách dùng (chạy trên máy có Internet, cùng Python của TV2):
    pip install requests python-dotenv
    python tools/coreiot_setup.py                       # chạy toàn bộ
    python tools/coreiot_setup.py --tv2-env ../BTL_IoT_TV2/.env
    python tools/coreiot_setup.py --assign-only         # chỉ chuyển Camera_AI_01 sang profile Camera_AI
    python tools/coreiot_setup.py --forbidden 8 17      # đặt khung giờ cấm 8h -> 17h (để demo ban ngày)

Thông tin đăng nhập đọc từ .env (COREIOT_URL, COREIOT_USERNAME, COREIOT_PASSWORD)
hoặc được hỏi trực tiếp trên terminal (mật khẩu không bị hiển thị).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coreiot_templates as t  # noqa: E402

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv là tuỳ chọn
    pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class CoreIOTError(RuntimeError):
    pass


class CoreIOT:
    """Client REST tối giản cho CoreIOT/ThingsBoard."""

    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.timeout = timeout

    # ── HTTP helpers ────────────────────────────────────────────────────
    def _req(self, method: str, path: str, *, ok404: bool = False, **kw):
        r = self.s.request(method, self.base + path, timeout=self.timeout, **kw)
        if ok404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise CoreIOTError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def get(self, path, **kw):
        return self._req("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self._req("POST", path, json=body, **kw)

    def delete(self, path, **kw):
        return self._req("DELETE", path, **kw)

    def login(self, username: str, password: str) -> None:
        data = self.post("/api/auth/login", {"username": username, "password": password})
        self.s.headers["X-Authorization"] = f"Bearer {data['token']}"

    def find_in_page(self, path: str, name_field: str, name: str) -> dict | None:
        """Tìm thực thể theo tên chính xác trong API phân trang."""
        sep = "&" if "?" in path else "?"
        page = 0
        while True:
            data = self.get(
                f"{path}{sep}pageSize=100&page={page}&textSearch={requests.utils.quote(name)}"
            )
            for item in data.get("data", []):
                if item.get(name_field) == name:
                    return item
            if not data.get("hasNext"):
                return None
            page += 1

    # ── Devices ─────────────────────────────────────────────────────────
    def get_device(self, name: str) -> dict | None:
        return self.get(
            f"/api/tenant/devices?deviceName={requests.utils.quote(name)}", ok404=True
        )


# ─────────────────────────────────────────────────────────────────────────────
# Các bước
# ─────────────────────────────────────────────────────────────────────────────

def step_gateway(api: CoreIOT) -> str:
    print(f"\n[T15] Thiết bị Gateway '{t.GATEWAY_NAME}'")
    dev = api.get_device(t.GATEWAY_NAME)
    if dev is None:
        dev = api.post(
            "/api/device",
            {
                "name": t.GATEWAY_NAME,
                "label": "PC Gateway – BTL IoT",
                "additionalInfo": {
                    "gateway": True,
                    "overwriteActivityTime": False,
                    "description": "IoT Gateway chạy trên Laptop (TV2) – YOLOv8 + Gemini",
                },
            },
        )
        print("  ✔ Đã tạo mới Gateway (Is gateway = true)")
    else:
        info = dev.get("additionalInfo") or {}
        if not info.get("gateway"):
            info["gateway"] = True
            info.setdefault("overwriteActivityTime", False)
            dev["additionalInfo"] = info
            dev = api.post("/api/device", dev)
            print("  ✔ Đã bật cờ 'Is gateway' cho thiết bị có sẵn")
        else:
            print("  • Gateway đã tồn tại – giữ nguyên")

    creds = api.get(f"/api/device/{dev['id']['id']}/credentials")
    token = creds.get("credentialsId", "")
    print(f"  ✔ Access Token: {token}")
    return token


def step_notification(api: CoreIOT) -> tuple[str | None, list[str]]:
    print("\n[T13] Notification target & template")
    try:
        target = api.find_in_page("/api/notification/targets", "name", t.NOTIFICATION_TARGET_NAME)
        if target is None:
            target = api.post("/api/notification/target", t.build_notification_target())
            print("  ✔ Đã tạo Recipients:", t.NOTIFICATION_TARGET_NAME)
        else:
            print("  • Recipients đã tồn tại")

        tpl = api.find_in_page(
            "/api/notification/templates?notificationTypes=RULE_NODE", "name", t.NOTIFICATION_TEMPLATE_NAME
        )
        body = t.build_notification_template()
        if tpl is None:
            tpl = api.post("/api/notification/template", body)
            print("  ✔ Đã tạo Template:", t.NOTIFICATION_TEMPLATE_NAME)
        else:
            full = api.get(f"/api/notification/template/{tpl['id']['id']}")
            full["configuration"] = body["configuration"]
            tpl = api.post("/api/notification/template", full)
            print("  • Template đã tồn tại – đã cập nhật nội dung")
        return tpl["id"]["id"], [target["id"]["id"]]
    except CoreIOTError as exc:
        print("  ⚠ Không tạo được Notification (phiên bản CoreIOT cũ hoặc thiếu quyền):", exc)
        print("    -> Rule chain vẫn tạo Alarm; thêm node Send Notification bằng tay theo README.")
        return None, []


def step_rule_chain(api: CoreIOT, template_id: str | None, target_ids: list[str]) -> dict:
    print(f"\n[T12] Rule Chain '{t.RULE_CHAIN_NAME}'")
    export = t.build_rule_chain_export(template_id, target_ids)
    rc = api.find_in_page("/api/ruleChains?type=CORE", "name", t.RULE_CHAIN_NAME)
    if rc is None:
        rc_body = dict(export["ruleChain"])
        rc = api.post("/api/ruleChain", rc_body)
        print("  ✔ Đã tạo Rule Chain")
    else:
        print("  • Rule Chain đã tồn tại – ghi đè nội dung (metadata)")

    meta = dict(export["metadata"])
    meta["ruleChainId"] = rc["id"]
    api.post("/api/ruleChain/metadata", meta)
    n = len(meta["nodes"])
    print(f"  ✔ Đã lưu {n} node, {len(meta['connections'])} kết nối"
          + (" (có Send Notification)" if template_id else " (chưa có Send Notification)"))
    return rc


def step_device_profile(api: CoreIOT, rule_chain: dict) -> dict:
    print(f"\n[T12] Device profile '{t.DEVICE_PROFILE_NAME}' -> rule chain '{t.RULE_CHAIN_NAME}'")
    prof = api.find_in_page("/api/deviceProfiles", "name", t.DEVICE_PROFILE_NAME)
    if prof is None:
        prof = api.post(
            "/api/deviceProfile",
            {
                "name": t.DEVICE_PROFILE_NAME,
                "type": "DEFAULT",
                "transportType": "DEFAULT",
                "provisionType": "DISABLED",
                "description": "Camera AI (YOLOv8) gửi dữ liệu qua BTL_PC_GATEWAY",
                "defaultRuleChainId": rule_chain["id"],
                "profileData": {
                    "configuration": {"type": "DEFAULT"},
                    "transportConfiguration": {"type": "DEFAULT"},
                    "provisionConfiguration": {"type": "DISABLED", "provisionDeviceSecret": None},
                    "alarms": None,
                },
            },
        )
        print("  ✔ Đã tạo Device profile")
    else:
        prof = api.get(f"/api/deviceProfile/{prof['id']['id']}")
        prof["defaultRuleChainId"] = rule_chain["id"]
        prof = api.post("/api/deviceProfile", prof)
        print("  ✔ Đã gán Default rule chain cho profile có sẵn")
    return prof


def step_assign_device(api: CoreIOT, profile: dict | None) -> dict | None:
    print(f"\n[T15] Thiết bị con '{t.DEVICE_NAME}'")
    dev = api.get_device(t.DEVICE_NAME)
    if dev is None:
        print("  • Chưa có – CoreIOT sẽ tự sinh khi Gateway gửi v1/gateway/connect.")
        print("    Sau lần chạy Gateway đầu tiên, chạy lại: python tools/coreiot_setup.py --assign-only")
        return None
    if profile is None:
        profile = api.find_in_page("/api/deviceProfiles", "name", t.DEVICE_PROFILE_NAME)
        if profile is None:
            raise CoreIOTError(f"Chưa có Device profile '{t.DEVICE_PROFILE_NAME}'. Hãy chạy setup đầy đủ trước.")
    if dev.get("deviceProfileId", {}).get("id") == profile["id"]["id"]:
        print(f"  • Đã thuộc profile '{t.DEVICE_PROFILE_NAME}'")
    else:
        dev["deviceProfileId"] = profile["id"]
        dev.pop("type", None)
        dev = api.post("/api/device", dev)
        print(f"  ✔ Đã chuyển sang profile '{t.DEVICE_PROFILE_NAME}' (dùng Rule Chain cảnh báo)")
    return dev


def step_forbidden_hours(api: CoreIOT, device: dict | None, start: int, end: int) -> None:
    if device is None:
        print("  ⚠ Chưa có Camera_AI_01 nên chưa ghi được khung giờ cấm (rule chain dùng mặc định 22h–6h).")
        return
    api.post(
        f"/api/plugins/telemetry/DEVICE/{device['id']['id']}/attributes/SERVER_SCOPE",
        {"forbidden_start_hour": start, "forbidden_end_hour": end},
    )
    print(f"  ✔ Khung giờ cấm (Server attributes): {start}h -> {end}h (giờ VN)")


def step_dashboard(api: CoreIOT, replace: bool) -> None:
    print(f"\n[T11] Dashboard '{t.DASHBOARD_TITLE}'")
    existing = api.find_in_page("/api/tenant/dashboards", "title", t.DASHBOARD_TITLE)
    if existing is not None:
        if not replace:
            print("  • Dashboard đã tồn tại – bỏ qua (dùng --replace-dashboard để tạo lại)")
            return
        api.delete(f"/api/dashboard/{existing['id']['id']}")
        print("  • Đã xoá dashboard cũ")
    dash = api.post("/api/dashboard", t.build_dashboard())
    print(f"  ✔ Đã tạo Dashboard: {api.base}/dashboards/{dash['id']['id']}")


def write_token_to_env(env_path: Path, token: str) -> None:
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    line = f"GATEWAY_ACCESS_TOKEN={token}"
    if re.search(r"^GATEWAY_ACCESS_TOKEN=.*$", text, flags=re.M):
        text = re.sub(r"^GATEWAY_ACCESS_TOKEN=.*$", line, text, flags=re.M)
    else:
        text = (text.rstrip("\n") + "\n" if text else "") + line + "\n"
    env_path.write_text(text, encoding="utf-8")
    print(f"  ✔ Đã ghi token vào {env_path}")


# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cấu hình CoreIOT cho BTL IoT (TV3)")
    ap.add_argument("--url", default=os.getenv("COREIOT_URL", "https://app.coreiot.io"))
    ap.add_argument("--username", default=os.getenv("COREIOT_USERNAME"))
    ap.add_argument("--password", default=os.getenv("COREIOT_PASSWORD"))
    ap.add_argument("--tv2-env", "--env", dest="tv2_env", type=Path,
                    help="Đường dẫn file .env để ghi GATEWAY_ACCESS_TOKEN (vd: .env)")
    ap.add_argument("--assign-only", action="store_true",
                    help="Chỉ chuyển Camera_AI_01 sang profile Camera_AI (+ khung giờ cấm nếu có --forbidden)")
    ap.add_argument("--forbidden", nargs=2, type=int, metavar=("START", "END"),
                    help="Khung giờ cấm theo giờ VN, vd: --forbidden 22 6")
    ap.add_argument("--replace-dashboard", action="store_true")
    ap.add_argument("--no-dashboard", action="store_true")
    args = ap.parse_args(argv)

    username = args.username or input("CoreIOT email: ").strip()
    password = args.password or getpass.getpass("CoreIOT mật khẩu: ")

    api = CoreIOT(args.url)
    try:
        api.login(username, password)
        print(f"Đăng nhập {args.url} thành công.")

        if args.assign_only:
            dev = step_assign_device(api, None)
            if args.forbidden:
                step_forbidden_hours(api, dev, *args.forbidden)
            return 0

        token = step_gateway(api)
        if args.tv2_env:
            write_token_to_env(args.tv2_env, token)

        tpl_id, targets = step_notification(api)
        rc = step_rule_chain(api, tpl_id, targets)
        prof = step_device_profile(api, rc)
        dev = step_assign_device(api, prof)
        if args.forbidden:
            step_forbidden_hours(api, dev, *args.forbidden)
        if not args.no_dashboard:
            step_dashboard(api, args.replace_dashboard)

        print("\nHOÀN TẤT. Gửi Access Token ở trên cho TV2 (hoặc nhập tại http://localhost:8000).")
        return 0
    except (CoreIOTError, requests.RequestException, KeyError) as exc:
        print("\n✖ Lỗi:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
