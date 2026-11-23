// T12 – Có người xuất hiện trong khung giờ cấm?
// Khung giờ cấm đọc từ Server attributes (forbidden_start_hour, forbidden_end_hour),
// nếu chưa đặt thì dùng mặc định 22h -> 6h (giờ Việt Nam).
var count = Number(msg.people_count);
if (isNaN(count)) { count = 0; }

var startH = parseInt(metadata.ss_forbidden_start_hour);
var endH = parseInt(metadata.ss_forbidden_end_hour);
if (isNaN(startH)) { startH = 22; }
if (isNaN(endH)) { endH = 6; }

// metadata.ts = thời điểm đo (ts do Gateway gửi lên, mili-giây UTC)
var ts = parseInt(metadata.ts);
if (isNaN(ts)) { ts = Date.now(); }
var hourVN = Math.floor(ts / 3600000 + 7) % 24;

var inForbidden;
if (startH === endH) {
    inForbidden = true;                                  // cấm cả ngày
} else if (startH < endH) {
    inForbidden = hourVN >= startH && hourVN < endH;     // vd 8h -> 17h
} else {
    inForbidden = hourVN >= startH || hourVN < endH;     // qua nửa đêm, vd 22h -> 6h
}

return count > 0 && inForbidden;
