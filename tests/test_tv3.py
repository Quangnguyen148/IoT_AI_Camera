"""
test_tv3.py – Kiểm thử phần việc TV3 (không cần Internet)

    python -m unittest discover -s tests -v

1. Logic khung giờ cấm của RuleChain (chạy script JS thật bằng Node.js nếu máy có `node`).
2. Cấu trúc JSON Rule Chain / Dashboard hợp lệ & khớp Hợp đồng dữ liệu.
3. coreiot_setup.py chạy trọn luồng với một CoreIOT REST API giả lập.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import coreiot_templates as t  # noqa: E402

NODE = shutil.which("node")
VN = timezone(timedelta(hours=7))


def run_js(body: str, msg: dict, metadata: dict, msg_type: str = "POST_TELEMETRY_REQUEST"):
    """Chạy script giống cách ThingsBoard bọc: function(msg, metadata, msgType) { <body> }"""
    program = (
        f"var f = function(msg, metadata, msgType) {{\n{body}\n}};\n"
        f"console.log(JSON.stringify(f({json.dumps(msg)}, {json.dumps(metadata)}, {json.dumps(msg_type)})));"
    )
    out = subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def ts_vn(hour: int, minute: int = 30) -> str:
    return str(int(datetime(2026, 11, 21, hour, minute, tzinfo=VN).timestamp() * 1000))


@unittest.skipIf(NODE is None, "Cần Node.js để chạy script RuleChain")
class TestRuleChainScripts(unittest.TestCase):
    def check(self, people, hour, expected, start=None, end=None):
        md = {"ts": ts_vn(hour), "deviceName": t.DEVICE_NAME}
        if start is not None:
            md["ss_forbidden_start_hour"] = str(start)
            md["ss_forbidden_end_hour"] = str(end)
        got = run_js(t.FORBIDDEN_FILTER_JS, {"people_count": people}, md)
        self.assertIs(got, expected, f"people={people} hour={hour} window={start}-{end}")

    def test_default_window_22_to_6(self):
        self.check(2, 23, True)
        self.check(1, 2, True)
        self.check(1, 5, True)
        self.check(1, 6, False)
        self.check(3, 14, False)
        self.check(1, 21, False)
        self.check(1, 22, True)
        self.check(0, 23, False)  # không có người -> không cảnh báo

    def test_custom_day_window_from_attributes(self):
        self.check(1, 10, True, start=8, end=17)
        self.check(1, 17, False, start=8, end=17)
        self.check(1, 7, False, start=8, end=17)

    def test_missing_people_count_is_false(self):
        got = run_js(t.FORBIDDEN_FILTER_JS, {"other": 1}, {"ts": ts_vn(23)})
        self.assertIs(got, False)

    def test_alarm_details_accumulate(self):
        d1 = run_js(t.CREATE_ALARM_DETAILS_JS,
                    {"people_count": 2, "llm_alert": "Có 2 người lạ", "anomaly_detected": True},
                    {"ts": ts_vn(23)})
        self.assertEqual(d1["people_count"], 2)
        self.assertEqual(d1["max_people"], 2)
        self.assertEqual(d1["hour_vn"], 23)
        d2 = run_js(t.CREATE_ALARM_DETAILS_JS,
                    {"people_count": 1, "llm_alert": "", "anomaly_detected": True},
                    {"ts": ts_vn(23), "prevAlarmDetails": json.dumps(d1)})
        self.assertEqual(d2["max_people"], 2)
        self.assertEqual(d2["llm_alert"], "Có 2 người lạ")
        self.assertEqual(d2["count_updates"], 2)

    def test_notify_transform_exposes_template_vars(self):
        alarm = {"type": t.ALARM_TYPE, "severity": "CRITICAL",
                 "details": {"people_count": 2, "llm_alert": "Có 2 người", "hour_vn": 23}}
        out = run_js(t.NOTIFY_TRANSFORM_JS, alarm, {"deviceName": t.DEVICE_NAME})
        md = out["metadata"]
        for var in ("people_count", "llm_alert", "hour_vn", "deviceName"):
            self.assertIn("${" + var + "}", t.NOTIFICATION_SUBJECT + t.NOTIFICATION_BODY)
            self.assertIsInstance(md[var], str)
        self.assertEqual(md["people_count"], "2")


class TestJsonTemplates(unittest.TestCase):
    def test_rule_chain_structure(self):
        for with_notify in (False, True):
            exp = t.build_rule_chain_export(
                str(uuid.uuid4()) if with_notify else None, [str(uuid.uuid4())] if with_notify else None
            )
            json.dumps(exp)  # serializable
            nodes = exp["metadata"]["nodes"]
            conns = exp["metadata"]["connections"]
            for c in conns:
                self.assertLess(c["fromIndex"], len(nodes))
                self.assertLess(c["toIndex"], len(nodes))
            types = {(c["fromIndex"], c["type"]) for c in conns}
            self.assertIn((0, "Post telemetry"), types)
            self.assertIn((0, "RPC Request to Device"), types)  # bắt buộc để Switch RPC không timeout
            has_notify = any(n["type"].endswith("TbNotificationNode") for n in nodes)
            self.assertEqual(has_notify, with_notify)

    def test_dashboard_contract(self):
        d = t.build_dashboard()
        text = json.dumps(d, ensure_ascii=False)
        for key in ("people_count", "anomaly_detected", "llm_alert", "set_ai_status", "get_ai_status",
                    t.DEVICE_NAME):
            self.assertIn(key, text)
        cfg = d["configuration"]
        layout = cfg["states"]["default"]["layouts"]["main"]["widgets"]
        self.assertEqual(set(layout), set(cfg["widgets"]))
        for wid, w in cfg["widgets"].items():
            self.assertEqual(w["id"], wid)
            for ds in w["config"].get("datasources", []):
                self.assertIn(ds["entityAliasId"], cfg["entityAliases"])


# ─────────────────────────────── Fake CoreIOT ───────────────────────────────

class FakeTB:
    def __init__(self):
        self.devices, self.profiles, self.chains, self.dashboards = {}, {}, {}, {}
        self.targets, self.templates, self.metadata, self.attrs = {}, {}, {}, {}

    @staticmethod
    def new_id(kind):
        return {"id": str(uuid.uuid4()), "entityType": kind}


def make_handler(tb: FakeTB):
    def page(items):
        return {"data": list(items), "totalPages": 1, "totalElements": len(items), "hasNext": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj=None):
            body = b"" if obj is None else json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            if self.headers.get("X-Authorization") != "Bearer FAKE":
                self._send(401, {"message": "unauthorized"})
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            if p == "/api/tenant/devices" and "deviceName" in q:
                d = next((d for d in tb.devices.values() if d["name"] == q["deviceName"]), None)
                return self._send(200, d) if d else self._send(404, {"message": "not found"})
            if p.startswith("/api/device/") and p.endswith("/credentials"):
                return self._send(200, {"credentialsId": "TOKEN_" + p.split("/")[3][:8]})
            if p == "/api/notification/targets":
                return self._send(200, page(tb.targets.values()))
            if p == "/api/notification/templates":
                return self._send(200, page(tb.templates.values()))
            if p.startswith("/api/notification/template/"):
                return self._send(200, tb.templates[p.rsplit("/", 1)[1]])
            if p == "/api/ruleChains":
                return self._send(200, page(tb.chains.values()))
            if p == "/api/deviceProfiles":
                return self._send(200, page(tb.profiles.values()))
            if p.startswith("/api/deviceProfile/"):
                return self._send(200, tb.profiles[p.rsplit("/", 1)[1]])
            if p == "/api/tenant/dashboards":
                return self._send(200, page(tb.dashboards.values()))
            self._send(404, {"message": "no route " + p})

        def do_DELETE(self):
            if not self._auth():
                return
            tb.dashboards.pop(self.path.rsplit("/", 1)[1], None)
            self._send(200)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"null")
            p = urlparse(self.path).path
            if p == "/api/auth/login":
                ok = body == {"username": "tv3@hcmut.edu.vn", "password": "secret"}
                return self._send(200, {"token": "FAKE"}) if ok else self._send(401, {})
            if not self._auth():
                return

            def save(store, kind, obj):
                obj.setdefault("id", FakeTB.new_id(kind))
                store[obj["id"]["id"]] = obj
                return self._send(200, obj)

            if p == "/api/device":
                body.setdefault("deviceProfileId", {"id": "default-profile", "entityType": "DEVICE_PROFILE"})
                return save(tb.devices, "DEVICE", body)
            if p == "/api/notification/target":
                return save(tb.targets, "NOTIFICATION_TARGET", body)
            if p == "/api/notification/template":
                return save(tb.templates, "NOTIFICATION_TEMPLATE", body)
            if p == "/api/ruleChain":
                return save(tb.chains, "RULE_CHAIN", body)
            if p == "/api/ruleChain/metadata":
                tb.metadata[body["ruleChainId"]["id"]] = body
                return self._send(200, body)
            if p == "/api/deviceProfile":
                return save(tb.profiles, "DEVICE_PROFILE", body)
            if p == "/api/dashboard":
                return save(tb.dashboards, "DASHBOARD", body)
            if "/attributes/SERVER_SCOPE" in p:
                tb.attrs[p.split("/")[5]] = body
                return self._send(200)
            self._send(404, {"message": "no route " + p})

    return H


class TestSetupScript(unittest.TestCase):
    def setUp(self):
        self.tb = FakeTB()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.tb))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.srv.shutdown()
        self.tmp.cleanup()

    def run_setup(self, *extra):
        import coreiot_setup

        return coreiot_setup.main(["--url", self.url, "--username", "tv3@hcmut.edu.vn",
                                   "--password", "secret", *extra])

    def test_full_flow_and_idempotent(self):
        env = Path(self.tmp.name) / ".env"
        env.write_text("COREIOT_HOST=app.coreiot.io\nGATEWAY_ACCESS_TOKEN=YOUR_TOKEN\n", encoding="utf-8")

        self.assertEqual(self.run_setup("--tv2-env", str(env)), 0)
        gw = next(d for d in self.tb.devices.values() if d["name"] == t.GATEWAY_NAME)
        self.assertTrue(gw["additionalInfo"]["gateway"])
        self.assertIn("GATEWAY_ACCESS_TOKEN=TOKEN_", env.read_text(encoding="utf-8"))
        self.assertIn("COREIOT_HOST=app.coreiot.io", env.read_text(encoding="utf-8"))

        (meta,) = self.tb.metadata.values()
        notify = [n for n in meta["nodes"] if n["type"].endswith("TbNotificationNode")]
        self.assertEqual(len(notify), 1)
        self.assertIn(notify[0]["configuration"]["templateId"], self.tb.templates)
        (prof,) = self.tb.profiles.values()
        self.assertEqual(prof["defaultRuleChainId"]["id"], meta["ruleChainId"]["id"])
        self.assertEqual(len(self.tb.dashboards), 1)

        # Chạy lại: không nhân bản thực thể
        self.assertEqual(self.run_setup(), 0)
        self.assertEqual(len(self.tb.devices), 1)
        self.assertEqual(len(self.tb.chains), 1)
        self.assertEqual(len(self.tb.profiles), 1)
        self.assertEqual(len(self.tb.templates), 1)
        self.assertEqual(len(self.tb.dashboards), 1)

    def test_assign_only_after_gateway_autocreates_device(self):
        self.assertEqual(self.run_setup("--no-dashboard"), 0)
        # Giả lập CoreIOT tự sinh Camera_AI_01 khi Gateway gửi v1/gateway/connect
        cam = {"id": FakeTB.new_id("DEVICE"), "name": t.DEVICE_NAME, "type": "default",
               "deviceProfileId": {"id": "default-profile", "entityType": "DEVICE_PROFILE"}}
        self.tb.devices[cam["id"]["id"]] = cam

        self.assertEqual(self.run_setup("--assign-only", "--forbidden", "8", "17"), 0)
        (prof,) = self.tb.profiles.values()
        cam = self.tb.devices[cam["id"]["id"]]
        self.assertEqual(cam["deviceProfileId"]["id"], prof["id"]["id"])
        self.assertEqual(self.tb.attrs[cam["id"]["id"]],
                         {"forbidden_start_hour": 8, "forbidden_end_hour": 17})

    def test_wrong_password(self):
        import coreiot_setup

        rc = coreiot_setup.main(["--url", self.url, "--username", "x", "--password", "y"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
