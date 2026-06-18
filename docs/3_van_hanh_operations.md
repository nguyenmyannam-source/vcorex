# 3. Hướng Dẫn Vận Hành | 3. Operations Guide

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Tài liệu này hướng dẫn cách thức vận hành hệ thống hàng ngày, theo dõi và quản trị rủi ro cho bot VCOREX.

## 3.1. Hệ Thống Ghi Nhật Ký (Logging) Đã Tối Ưu

Bot sử dụng cấu trúc Log 3 luồng riêng biệt, được lưu trữ trong thư mục `logs/`:
- **`vcorex.log`**: Tệp nhật ký chính chứa toàn bộ diễn biến của hệ thống. Tệp này tự động xoay vòng (rotate) mỗi khi đạt dung lượng 50MB và được nén zip lại. Hệ thống chỉ giữ tối đa 5 tệp gần nhất để tiết kiệm dung lượng.
- **`errors.log`**: Chỉ lưu các dòng lỗi (ERROR, CRITICAL) kèm Lịch sử thực thi (Stacktrace) đầy đủ để dễ dàng dò tìm và gỡ lỗi (debug).
- **`trades.jsonl`**: Tệp kiểm toán giao dịch (Audit). Chỉ lưu lại các sự kiện như vào lệnh, chốt lời, cắt lỗ dưới định dạng JSON Lines. Dữ liệu này dễ dàng đưa vào Excel, Grafana hoặc Python để vẽ biểu đồ phân tích lợi nhuận (PnL).

## 3.2. Giám Sát Hệ Thống (Bot Monitor)

Sử dụng tệp mã kịch bản (script) giám sát có sẵn để theo dõi tình trạng sức khỏe của bot:
```powershell
python monitor_bot.py
```
Mã lệnh này sẽ hiển thị:
- Trạng thái tiến trình (PID, Thời gian chạy liên tục - Uptime).
- Tình trạng bộ nhớ (Mức sử dụng RAM).
- Cảnh báo tức thời nếu Bot bị sập (crash) hoặc treo.

## 3.3. Thông Báo Telegram & Biểu Đồ Trực Quan

Nếu bật tính năng `TELEGRAM_ENABLED=True` trong tệp `.env`, bot sẽ tự động gửi các báo cáo và cảnh báo sau:
- **Biểu đồ Trực quan hóa (Auto-Charts):** Bất cứ khi nào có tín hiệu vào lệnh được duyệt, bot sẽ tự động vẽ một biểu đồ hình ảnh (.png) chứa Đồ thị nến, Đường trung bình động EMA, Chỉ báo ADX, và đánh dấu chính xác điểm vào lệnh, rồi gửi trực tiếp qua Telegram. Các ảnh rác sinh ra trong quá trình tạo biểu đồ sẽ tự động được thu dọn để không làm tốn dung lượng máy chủ.
- **Cảnh báo lỗi nghiêm trọng:** Đứt kết nối luồng sự kiện WebSocket, Gọi API vượt quá giới hạn cho phép.
- **Báo cáo khớp lệnh:** Thông báo vào lệnh, trạng thái Chốt Lời (Take Profit) / Cắt Lỗ (Stop Loss). 
- **Báo cáo tài khoản định kỳ:** Thông tin số dư và tình trạng vị thế tổng thể.

## 3.4. Cấu Trúc Quản Trị Rủi Ro (Risk Management)

Trong tệp `.env`, bạn có thể thay đổi các biến số sau để can thiệp trực tiếp vào cách vào lệnh của bot:
- `MAX_OPEN_POSITIONS`: Số lượng lệnh tối đa được phép mở cùng lúc trên toàn bộ tài khoản.
- `MAX_SYMBOL_CONCENTRATION`: Số lượng lệnh tối đa được phép mở cùng lúc trên **một đồng tiền mã hóa** (Ví dụ: Đặt bằng `1` để tránh nhồi lệnh khi nhiều khung giờ cùng phát ra tín hiệu).
- `RISK_PER_TRADE_PERCENT`: Tỷ lệ phần trăm tài khoản tối đa có thể rủi ro cho mỗi lệnh (Dùng để tính toán Quy mô lệnh tự động - Position Sizing).

*(Lưu ý: Sau khi cập nhật tệp `.env`, bắt buộc phải khởi động lại bot để hệ thống tải cấu hình mới).*
