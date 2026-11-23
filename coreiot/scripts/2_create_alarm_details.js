// Chi tiết cảnh báo hiển thị trên CoreIOT (Alarm details)
var details = {};
if (metadata.prevAlarmDetails) {
    details = JSON.parse(metadata.prevAlarmDetails);
}
var count = Number(msg.people_count) || 0;
var ts = parseInt(metadata.ts);
if (isNaN(ts)) { ts = Date.now(); }
details.people_count = count;
details.max_people = Math.max(Number(details.max_people) || 0, count);
details.llm_alert = msg.llm_alert || details.llm_alert || '';
details.anomaly_detected = msg.anomaly_detected;
details.hour_vn = Math.floor(ts / 3600000 + 7) % 24;
details.count_updates = (Number(details.count_updates) || 0) + 1;
return details;
