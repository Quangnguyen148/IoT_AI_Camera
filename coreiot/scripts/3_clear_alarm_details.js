// Ghi lý do tự động xoá cảnh báo
var details = {};
if (metadata.prevAlarmDetails) {
    details = JSON.parse(metadata.prevAlarmDetails);
}
details.cleared_reason = 'Không còn người trong khung giờ cấm';
details.people_count_at_clear = Number(msg.people_count) || 0;
return details;
