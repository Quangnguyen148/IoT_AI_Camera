"""
export_templates.py – Xuất các file JSON/JS tĩnh vào thư mục coreiot/ để Import bằng tay trên CoreIOT.

    python tools/export_templates.py
"""

from __future__ import annotations

import json
from pathlib import Path

import coreiot_templates as t

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coreiot"


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  ✔", path.relative_to(ROOT))


def main() -> None:
    print("Xuất cấu hình CoreIOT (TV3)...")
    _dump(OUT / "rulechain_btl_camera_ai_security.json", t.build_rule_chain_export())
    _dump(OUT / "dashboard_btl_camera_ai.json", t.build_dashboard())
    _dump(OUT / "notification_template.json", t.build_notification_template())

    scripts = {
        "1_filter_khung_gio_cam.js": t.FORBIDDEN_FILTER_JS,
        "2_create_alarm_details.js": t.CREATE_ALARM_DETAILS_JS,
        "3_clear_alarm_details.js": t.CLEAR_ALARM_DETAILS_JS,
        "4_transform_thong_bao.js": t.NOTIFY_TRANSFORM_JS,
    }
    for name, code in scripts.items():
        p = OUT / "scripts" / name
        p.write_text(code, encoding="utf-8")
        print("  ✔", p.relative_to(ROOT))

    (OUT / "scripts" / "5_notification_text.txt").write_text(
        f"Subject:\n{t.NOTIFICATION_SUBJECT}\n\nMessage:\n{t.NOTIFICATION_BODY}\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
