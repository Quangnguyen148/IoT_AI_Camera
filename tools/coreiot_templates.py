"""
coreiot_templates.py – Nguồn duy nhất (single source of truth) cho cấu hình CoreIOT của TV3
==========================================================================================
Sinh ra:
  • Rule Chain "BTL Camera AI Security"  (T12 + T13)
  • Dashboard  "BTL IoT – Giám sát Camera AI" (T11)

Được dùng bởi:
  • export_templates.py  -> xuất file JSON tĩnh trong thư mục coreiot/ để Import bằng tay
  • coreiot_setup.py     -> tạo tự động qua REST API của CoreIOT (ThingsBoard)

Hợp đồng dữ liệu (theo Tài liệu đặc tả hệ thống):
  Gateway  : BTL_PC_GATEWAY
  Thiết bị : Camera_AI_01
  Telemetry: people_count (int), anomaly_detected (bool), llm_alert (str)
  RPC      : set_ai_status (params true/false), get_ai_status (params null)
"""

from __future__ import annotations

import copy
import uuid

GATEWAY_NAME = "BTL_PC_GATEWAY"
DEVICE_NAME = "Camera_AI_01"
DEVICE_PROFILE_NAME = "Camera_AI"
RULE_CHAIN_NAME = "BTL Camera AI Security"
DASHBOARD_TITLE = "BTL IoT – Giám sát Camera AI"
ALARM_TYPE = "Xâm nhập ngoài giờ"
NOTIFICATION_TEMPLATE_NAME = "BTL – Cảnh báo xâm nhập ngoài giờ"
NOTIFICATION_TARGET_NAME = "BTL – Tenant administrators"

# Khung giờ cấm mặc định (giờ Việt Nam, UTC+7). Có thể ghi đè bằng
# Server attributes của Camera_AI_01: forbidden_start_hour / forbidden_end_hour
DEFAULT_FORBIDDEN_START = 22
DEFAULT_FORBIDDEN_END = 6
TIMEZONE_OFFSET_HOURS = 7

# ─────────────────────────────────────────────────────────────────────────────
# Script dùng trong Rule Chain (JavaScript – tương thích mọi phiên bản ThingsBoard)
# ─────────────────────────────────────────────────────────────────────────────

FORBIDDEN_FILTER_JS = f"""// T12 – Có người xuất hiện trong khung giờ cấm?
// Khung giờ cấm đọc từ Server attributes (forbidden_start_hour, forbidden_end_hour),
// nếu chưa đặt thì dùng mặc định {DEFAULT_FORBIDDEN_START}h -> {DEFAULT_FORBIDDEN_END}h (giờ Việt Nam).
var count = Number(msg.people_count);
if (isNaN(count)) {{ count = 0; }}

var startH = parseInt(metadata.ss_forbidden_start_hour);
var endH = parseInt(metadata.ss_forbidden_end_hour);
if (isNaN(startH)) {{ startH = {DEFAULT_FORBIDDEN_START}; }}
if (isNaN(endH)) {{ endH = {DEFAULT_FORBIDDEN_END}; }}

// metadata.ts = thời điểm đo (ts do Gateway gửi lên, mili-giây UTC)
var ts = parseInt(metadata.ts);
if (isNaN(ts)) {{ ts = Date.now(); }}
var hourVN = Math.floor(ts / 3600000 + {TIMEZONE_OFFSET_HOURS}) % 24;

var inForbidden;
if (startH === endH) {{
    inForbidden = true;                                  // cấm cả ngày
}} else if (startH < endH) {{
    inForbidden = hourVN >= startH && hourVN < endH;     // vd 8h -> 17h
}} else {{
    inForbidden = hourVN >= startH || hourVN < endH;     // qua nửa đêm, vd 22h -> 6h
}}

return count > 0 && inForbidden;
"""

CREATE_ALARM_DETAILS_JS = f"""// Chi tiết cảnh báo hiển thị trên CoreIOT (Alarm details)
var details = {{}};
if (metadata.prevAlarmDetails) {{
    details = JSON.parse(metadata.prevAlarmDetails);
}}
var count = Number(msg.people_count) || 0;
var ts = parseInt(metadata.ts);
if (isNaN(ts)) {{ ts = Date.now(); }}
details.people_count = count;
details.max_people = Math.max(Number(details.max_people) || 0, count);
details.llm_alert = msg.llm_alert || details.llm_alert || '';
details.anomaly_detected = msg.anomaly_detected;
details.hour_vn = Math.floor(ts / 3600000 + {TIMEZONE_OFFSET_HOURS}) % 24;
details.count_updates = (Number(details.count_updates) || 0) + 1;
return details;
"""

CLEAR_ALARM_DETAILS_JS = """// Ghi lý do tự động xoá cảnh báo
var details = {};
if (metadata.prevAlarmDetails) {
    details = JSON.parse(metadata.prevAlarmDetails);
}
details.cleared_reason = 'Không còn người trong khung giờ cấm';
details.people_count_at_clear = Number(msg.people_count) || 0;
return details;
"""

NOTIFY_TRANSFORM_JS = """// T13 – Chuẩn bị biến cho mẫu thông báo (Notification template)
// msg lúc này là đối tượng Alarm vừa được tạo; đưa thông tin cần hiển thị vào metadata
var d = msg.details || {};
metadata.people_count = String(d.people_count !== undefined ? d.people_count : '');
metadata.llm_alert = d.llm_alert ? String(d.llm_alert) : 'Phát hiện người trong khung giờ cấm';
metadata.alarm_type = String(msg.type || '');
metadata.alarm_severity = String(msg.severity || '');
metadata.hour_vn = String(d.hour_vn !== undefined ? d.hour_vn : '');
return {msg: msg, metadata: metadata, msgType: msgType};
"""

NOTIFICATION_SUBJECT = "🚨 Xâm nhập ngoài giờ – ${deviceName}"
NOTIFICATION_BODY = (
    "Phát hiện ${people_count} người lúc ${hour_vn}h (khung giờ cấm). "
    "AI: ${llm_alert}"
)


def _js_node(script: str) -> dict:
    """Cấu hình chung cho node có script: chọn JS, kèm TBEL tương đương cho bản mới."""
    return {"scriptLang": "JS", "jsScript": script, "tbelScript": script}


# ─────────────────────────────────────────────────────────────────────────────
# RULE CHAIN (T12 + T13)
# ─────────────────────────────────────────────────────────────────────────────

def build_rule_chain_nodes(
    notification_template_id: str | None = None,
    notification_target_ids: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Trả về (nodes, connections) của rule chain.

    Nếu có notification_template_id + target thì thêm node Send Notification (T13).
    """

    def node(type_: str, name: str, x: int, y: int, config: dict) -> dict:
        return {
            "additionalInfo": {"description": "", "layoutX": x, "layoutY": y},
            "type": type_,
            "name": name,
            "debugMode": False,
            "singletonMode": False,
            "configuration": config,
        }

    nodes = [
        # 0
        node(
            "org.thingsboard.rule.engine.filter.TbMsgTypeSwitchNode",
            "Phân loại bản tin",
            60, 300, {"version": 0},
        ),
        # 1
        node(
            "org.thingsboard.rule.engine.telemetry.TbMsgTimeseriesNode",
            "Lưu Telemetry",
            360, 150, {"defaultTTL": 0},
        ),
        # 2
        node(
            "org.thingsboard.rule.engine.telemetry.TbMsgAttributesNode",
            "Lưu Client Attributes",
            360, 300, {"scope": "CLIENT_SCOPE", "notifyDevice": False},
        ),
        # 3
        node(
            "org.thingsboard.rule.engine.rpc.TbSendRPCRequestNode",
            "Gửi RPC xuống Gateway",
            360, 450, {"timeoutInSeconds": 60},
        ),
        # 4
        node(
            "org.thingsboard.rule.engine.metadata.TbGetAttributesNode",
            "Đọc khung giờ cấm",
            660, 150,
            {
                "tellFailureIfAbsent": False,
                "fetchTo": "METADATA",
                "clientAttributeNames": [],
                "sharedAttributeNames": [],
                "serverAttributeNames": ["forbidden_start_hour", "forbidden_end_hour"],
                "latestTsKeyNames": [],
                "getLatestValueWithTs": False,
            },
        ),
        # 5
        node(
            "org.thingsboard.rule.engine.filter.TbJsFilterNode",
            "Có người trong khung giờ cấm?",
            960, 150, _js_node(FORBIDDEN_FILTER_JS),
        ),
        # 6
        node(
            "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
            "Tạo cảnh báo xâm nhập",
            1260, 60,
            {
                "scriptLang": "JS",
                "alarmDetailsBuildJs": CREATE_ALARM_DETAILS_JS,
                "alarmDetailsBuildTbel": CREATE_ALARM_DETAILS_JS,
                "useMessageAlarmData": False,
                "overwriteAlarmDetails": False,
                "alarmType": ALARM_TYPE,
                "severity": "CRITICAL",
                "propagate": False,
                "relationTypes": [],
                "propagateToOwner": False,
                "propagateToOwnerHierarchy": False,
                "propagateToTenant": False,
                "dynamicSeverity": False,
            },
        ),
        # 7
        node(
            "org.thingsboard.rule.engine.action.TbClearAlarmNode",
            "Xoá cảnh báo xâm nhập",
            1260, 260,
            {
                "scriptLang": "JS",
                "alarmDetailsBuildJs": CLEAR_ALARM_DETAILS_JS,
                "alarmDetailsBuildTbel": CLEAR_ALARM_DETAILS_JS,
                "alarmType": ALARM_TYPE,
            },
        ),
        # 8
        node(
            "org.thingsboard.rule.engine.transform.TbTransformMsgNode",
            "Chuẩn bị nội dung thông báo",
            1560, 60, _js_node(NOTIFY_TRANSFORM_JS),
        ),
    ]

    connections = [
        {"fromIndex": 0, "toIndex": 1, "type": "Post telemetry"},
        {"fromIndex": 0, "toIndex": 2, "type": "Post attributes"},
        {"fromIndex": 0, "toIndex": 3, "type": "RPC Request to Device"},
        {"fromIndex": 1, "toIndex": 4, "type": "Success"},
        {"fromIndex": 4, "toIndex": 5, "type": "Success"},
        {"fromIndex": 5, "toIndex": 6, "type": "True"},
        {"fromIndex": 5, "toIndex": 7, "type": "False"},
        {"fromIndex": 6, "toIndex": 8, "type": "Created"},
    ]

    if notification_template_id and notification_target_ids:
        nodes.append(
            node(
                "org.thingsboard.rule.engine.notification.TbNotificationNode",
                "Send Notification – chuông đỏ",
                1860, 60,
                {
                    "templateId": notification_template_id,
                    "targets": list(notification_target_ids),
                },
            )
        )
        connections.append({"fromIndex": 8, "toIndex": len(nodes) - 1, "type": "Success"})

    return nodes, connections


def build_rule_chain_export(
    notification_template_id: str | None = None,
    notification_target_ids: list[str] | None = None,
) -> dict:
    """JSON đúng định dạng 'Export rule chain' của ThingsBoard/CoreIOT (dùng để Import)."""
    nodes, connections = build_rule_chain_nodes(notification_template_id, notification_target_ids)
    return {
        "ruleChain": {
            "additionalInfo": {
                "description": (
                    "BTL IoT – TV3. T12: tạo cảnh báo khi people_count > 0 trong khung giờ cấm; "
                    "T13: gửi notification (chuông đỏ). Gán làm Default rule chain của "
                    f"Device profile '{DEVICE_PROFILE_NAME}'."
                )
            },
            "name": RULE_CHAIN_NAME,
            "type": "CORE",
            "firstRuleNodeId": None,
            "root": False,
            "debugMode": False,
            "configuration": None,
        },
        "metadata": {
            "firstNodeIndex": 0,
            "nodes": nodes,
            "connections": connections,
            "ruleChainConnections": None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# NOTIFICATION (T13)
# ─────────────────────────────────────────────────────────────────────────────

def build_notification_template() -> dict:
    return {
        "name": NOTIFICATION_TEMPLATE_NAME,
        "notificationType": "RULE_NODE",
        "configuration": {
            "deliveryMethodsTemplates": {
                "WEB": {
                    "method": "WEB",
                    "enabled": True,
                    "subject": NOTIFICATION_SUBJECT,
                    "body": NOTIFICATION_BODY,
                    "additionalConfig": {
                        "icon": {"enabled": True, "icon": "notifications_active", "color": "#D12730"},
                        "actionButtonConfig": {"enabled": False},
                    },
                }
            }
        },
    }


def build_notification_target() -> dict:
    return {
        "name": NOTIFICATION_TARGET_NAME,
        "configuration": {
            "type": "PLATFORM_USERS",
            "usersFilter": {"type": "TENANT_ADMINISTRATORS"},
            "description": "Gửi tới quản trị viên tenant (tài khoản nhóm BTL)",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD (T11)
# ─────────────────────────────────────────────────────────────────────────────

# ID cố định để file JSON xuất ra ổn định giữa các lần chạy
_NS = uuid.UUID("6f1c2a52-1b7e-4a55-9a43-7f0f0b3c0001")


def _uid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


ALIAS_ID = _uid("alias-camera")


def _key(name: str, label: str, color: str, key_type: str = "timeseries", **extra) -> dict:
    k = {
        "name": name,
        "type": key_type,
        "label": label,
        "color": color,
        "settings": {},
        "_hash": (uuid.uuid5(_NS, name + label).int % 1000) / 1000.0,
    }
    k.update(extra)
    return k


def _datasource(keys: list[dict]) -> list[dict]:
    return [
        {
            "type": "entity",
            "name": None,
            "entityAliasId": ALIAS_ID,
            "filterId": None,
            "dataKeys": keys,
        }
    ]


def _widget(
    wid: str,
    fqn: str,
    widget_type: str,
    title: str,
    size: tuple[int, int],
    config: dict,
) -> dict:
    bundle, type_alias = fqn.split(".", 1)
    base_config = {
        "showTitle": True,
        "title": title,
        "backgroundColor": "#ffffff",
        "color": "rgba(0, 0, 0, 0.87)",
        "padding": "8px",
        "dropShadow": True,
        "enableFullscreen": True,
        "useDashboardTimewindow": True,
        "showLegend": False,
        "widgetStyle": {},
        "titleStyle": {"fontSize": "16px", "fontWeight": 400},
        "actions": {},
        "settings": {},
    }
    base_config.update(config)
    return {
        "id": wid,
        # ThingsBoard >= 3.6
        "typeFullFqn": f"system.{fqn}",
        # ThingsBoard < 3.6
        "isSystemType": True,
        "bundleAlias": bundle,
        "typeAlias": type_alias,
        "type": widget_type,
        "title": title,
        "image": None,
        "description": None,
        "sizeX": size[0],
        "sizeY": size[1],
        "config": base_config,
    }


def build_dashboard() -> dict:
    w_people = _uid("w-people-count")
    w_anomaly = _uid("w-anomaly")
    w_switch = _uid("w-switch")
    w_chart = _uid("w-chart")
    w_alarms = _uid("w-alarms")
    w_table = _uid("w-table")

    widgets = {
        # 1. Thẻ giá trị: số người hiện tại
        w_people: _widget(
            w_people, "cards.simple_card", "latest", "Số người hiện tại", (8, 4),
            {
                "datasources": _datasource([_key("people_count", "Số người", "#2196f3", decimals=0, units="người")]),
                "backgroundColor": "#1565c0",
                "color": "rgba(255, 255, 255, 0.87)",
                "padding": "16px",
                "settings": {"labelPosition": "top"},
                "units": "người",
                "decimals": 0,
            },
        ),
        # 2. Thẻ trạng thái bất thường
        w_anomaly: _widget(
            w_anomaly, "cards.simple_card", "latest", "Trạng thái an ninh", (8, 4),
            {
                "datasources": _datasource([
                    _key(
                        "anomaly_detected", "AI phát hiện", "#f44336",
                        usePostProcessing=True,
                        postFuncBody=(
                            "return (value === true || value === 'true') "
                            "? 'CÓ NGƯỜI' : 'Bình thường';"
                        ),
                    )
                ]),
                "backgroundColor": "#c62828",
                "color": "rgba(255, 255, 255, 0.87)",
                "padding": "16px",
                "settings": {"labelPosition": "top"},
            },
        ),
        # 3. Công tắc RPC bật/tắt tính năng an ninh AI
        w_switch: _widget(
            w_switch, "control_widgets.switch_control", "rpc", "Kích hoạt an ninh AI", (8, 4),
            {
                "datasources": [],
                "targetDeviceAliases": [],
                "targetDeviceAliasIds": [ALIAS_ID],               # ThingsBoard < 3.7
                "targetDevice": {"type": "entity", "entityAliasId": ALIAS_ID},  # ThingsBoard >= 3.7
                "settings": {
                    "title": "Chế độ an ninh AI (set_ai_status)",
                    "showOnOffLabels": True,
                    "initialValue": True,
                    "retrieveValueMethod": "rpc",
                    "valueKey": "value",
                    "getValueMethod": "get_ai_status",
                    "parseValueFunction": "return data === true || data === 'true';",
                    "setValueMethod": "set_ai_status",
                    "convertValueFunction": "return value;",
                    "requestTimeout": 10000,
                    "requestPersistent": False,
                },
            },
        ),
        # 4. Đồ thị chuỗi thời gian lượng người
        w_chart: _widget(
            w_chart, "charts.basic_timeseries", "timeseries", "Lượng người theo thời gian", (15, 8),
            {
                "datasources": _datasource([
                    _key(
                        "people_count", "Số người", "#2196f3",
                        settings={"showLines": True, "fillLines": True, "showPoints": True, "lineWidth": 2},
                    )
                ]),
                "useDashboardTimewindow": False,
                "timewindow": {
                    "realtime": {"realtimeType": 0, "timewindowMs": 1800000, "interval": 5000},
                    "aggregation": {"type": "NONE", "limit": 25000},
                },
                "showLegend": True,
                "legendConfig": {"direction": "column", "position": "bottom", "showMin": True,
                                 "showMax": True, "showAvg": True, "showTotal": False},
                "settings": {
                    "shadowSize": 4,
                    "fontColor": "#545454",
                    "fontSize": 10,
                    "stack": False,
                    "smoothLines": False,
                    "tooltipIndividual": False,
                    "xaxis": {"showLabels": True, "color": "#545454"},
                    "yaxis": {"showLabels": True, "color": "#545454", "min": 0, "tickDecimals": 0,
                              "title": "Số người"},
                    "grid": {"verticalLines": True, "horizontalLines": True, "outlineWidth": 1,
                             "color": "#545454", "backgroundColor": None, "tickColor": "#DDDDDD"},
                },
            },
        ),
        # 5. Bảng cảnh báo (Alarm) do RuleChain sinh ra
        w_alarms: _widget(
            w_alarms, "alarm_widgets.alarms_table", "alarm", "Cảnh báo xâm nhập (RuleChain)", (9, 8),
            {
                "datasources": [],
                "alarmSource": {
                    "type": "entity",
                    "name": None,
                    "entityAliasId": ALIAS_ID,
                    "filterId": None,
                    "dataKeys": [
                        _key("createdTime", "Thời gian", "#2196f3", "alarm"),
                        _key("type", "Loại", "#f44336", "alarm"),
                        _key("severity", "Mức độ", "#ffc107", "alarm"),
                        _key("status", "Trạng thái", "#607d8b", "alarm"),
                    ],
                },
                "alarmSearchStatus": "ANY",
                "alarmStatusList": [],
                "alarmSeverityList": [],
                "alarmTypeList": [],
                "searchPropagatedAlarms": False,
                "alarmFilterConfig": {"statusList": [], "severityList": [], "typeList": [],
                                      "searchPropagatedAlarms": False},
                "alarmsPollingInterval": 5,
                "useDashboardTimewindow": False,
                "timewindow": {"realtime": {"realtimeType": 0, "timewindowMs": 86400000, "interval": 1000}},
                "settings": {
                    "enableSelection": True,
                    "enableSearch": True,
                    "displayDetails": True,
                    "allowAcknowledgment": True,
                    "allowClear": True,
                    "displayPagination": True,
                    "defaultPageSize": 10,
                    "defaultSortOrder": "-createdTime",
                    "enableSelectColumnDisplay": True,
                },
            },
        ),
        # 6. Lịch sử cảnh báo LLM
        w_table: _widget(
            w_table, "cards.timeseries_table", "timeseries", "Lịch sử AI & cảnh báo LLM", (24, 7),
            {
                "datasources": _datasource([
                    _key("people_count", "Số người", "#2196f3"),
                    _key("anomaly_detected", "Bất thường", "#f44336"),
                    _key("llm_alert", "Cảnh báo LLM (Gemini)", "#4caf50"),
                ]),
                "useDashboardTimewindow": False,
                "timewindow": {
                    "realtime": {"realtimeType": 0, "timewindowMs": 3600000, "interval": 5000},
                    "aggregation": {"type": "NONE", "limit": 500},
                },
                "settings": {
                    "showTimestamp": True,
                    "displayPagination": True,
                    "defaultPageSize": 10,
                    "enableSearch": True,
                    "hideEmptyLines": True,
                },
            },
        ),
    }

    layout = {
        w_people: {"sizeX": 8, "sizeY": 4, "row": 0, "col": 0},
        w_anomaly: {"sizeX": 8, "sizeY": 4, "row": 0, "col": 8},
        w_switch: {"sizeX": 8, "sizeY": 4, "row": 0, "col": 16},
        w_chart: {"sizeX": 15, "sizeY": 8, "row": 4, "col": 0},
        w_alarms: {"sizeX": 9, "sizeY": 8, "row": 4, "col": 15},
        w_table: {"sizeX": 24, "sizeY": 7, "row": 12, "col": 0},
    }
    for wid, pos in layout.items():
        widgets[wid]["row"] = pos["row"]
        widgets[wid]["col"] = pos["col"]

    return {
        "title": DASHBOARD_TITLE,
        "image": None,
        "mobileHide": False,
        "mobileOrder": None,
        "configuration": {
            "description": "T11 – Giám sát số người (YOLOv8 + Gemini) từ Camera_AI_01 qua BTL_PC_GATEWAY",
            "widgets": widgets,
            "states": {
                "default": {
                    "name": DASHBOARD_TITLE,
                    "root": True,
                    "layouts": {
                        "main": {
                            "widgets": copy.deepcopy(layout),
                            "gridSettings": {
                                "backgroundColor": "#eceff1",
                                "columns": 24,
                                "margin": 10,
                                "outerMargin": True,
                                "backgroundSizeMode": "100%",
                            },
                        }
                    },
                }
            },
            "entityAliases": {
                ALIAS_ID: {
                    "id": ALIAS_ID,
                    "alias": DEVICE_NAME,
                    "filter": {
                        "type": "entityName",
                        "resolveMultiple": False,
                        "entityType": "DEVICE",
                        "entityNameFilter": DEVICE_NAME,
                    },
                }
            },
            "filters": {},
            "timewindow": {
                "displayValue": "",
                "selectedTab": 0,
                "realtime": {"realtimeType": 0, "interval": 5000, "timewindowMs": 1800000},
                "history": {"historyType": 0, "interval": 5000, "timewindowMs": 3600000},
                "aggregation": {"type": "NONE", "limit": 25000},
            },
            "settings": {
                "stateControllerId": "entity",
                "showTitle": True,
                "showDashboardsSelect": True,
                "showEntitiesSelect": True,
                "showDashboardTimewindow": True,
                "showDashboardExport": True,
                "toolbarAlwaysOpen": True,
            },
        },
        "name": DASHBOARD_TITLE,
    }
