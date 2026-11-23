// T13 – Chuẩn bị biến cho mẫu thông báo (Notification template)
// msg lúc này là đối tượng Alarm vừa được tạo; đưa thông tin cần hiển thị vào metadata
var d = msg.details || {};
metadata.people_count = String(d.people_count !== undefined ? d.people_count : '');
metadata.llm_alert = d.llm_alert ? String(d.llm_alert) : 'Phát hiện người trong khung giờ cấm';
metadata.alarm_type = String(msg.type || '');
metadata.alarm_severity = String(msg.severity || '');
metadata.hour_vn = String(d.hour_vn !== undefined ? d.hour_vn : '');
return {msg: msg, metadata: metadata, msgType: msgType};
