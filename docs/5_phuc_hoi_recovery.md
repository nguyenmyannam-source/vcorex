# 5. Phục Hồi Sự Cố | 5. Incident Recovery

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Hệ thống VCOREX được thiết kế theo tiêu chuẩn tổ chức tài chính (Institutional-grade), đảm bảo khả năng tự phục hồi mạnh mẽ sau các sự cố gián đoạn mạng lưới hoặc lỗi phản hồi từ API của sàn giao dịch.

## 5.1. Tự Động Khôi Phục Kết Nối (WebSocket Auto-Recovery)

Giao thức WebSocket của sàn OKX yêu cầu tín hiệu nhịp tim (Heartbeat/Ping-Pong) định kỳ 20-30 giây để duy trì kết nối. Nếu đường truyền mạng gián đoạn khiến hệ thống không nhận được phản hồi:
- **Trình Giám Sát (Watchdog):** Chạy ngầm liên tục mỗi 5 giây, kiểm tra mốc thời gian nhận dữ liệu cuối cùng (`_last_receive_ts`).
- Nếu vượt quá 180 giây không có luồng dữ liệu (hoặc tùy thuộc vào độ rộng của khung thời gian nến), Watchdog sẽ ép buộc ngắt kết nối cũ và tự động khởi tạo kết nối mới (`reconnect`).
- Hệ thống hoàn toàn không bị mất dữ liệu. Ngay sau khi tái kết nối, bot sẽ tự động gọi REST API để "vá lỗ hổng" (Hydration) cho đoạn dữ liệu nến bị thiếu hụt trong khoảng thời gian rớt mạng.

## 5.2. Quản Lý Giới Hạn Tốc Độ (Rate Limit & Chống Khóa IP)

- Cơ chế **Hộp Thẻ Phạt (TokenBucketLimiter)** giới hạn luồng yêu cầu ở mức tối đa 50 yêu cầu/giây đối với REST API (Tuân thủ nghiêm ngặt giới hạn của OKX).
- Bot phân biệt rạch ròi các ranh giới tốc độ: Dữ liệu công khai `/api/v5/public` (Public Rate Limit) và Dữ liệu giao dịch `/api/v5/trade` (Private Rate Limit) nhằm đảm bảo hoạt động giao dịch không bao giờ bị nghẽn vì tải dữ liệu thị trường.

## 5.3. Xử Lý Lỗi API Máy Chủ Khách Khách Kháng (HTTP 50x Errors)

- Nếu sàn OKX bước vào giai đoạn bảo trì hoặc trả về mã lỗi HTTP 500 (Lỗi máy chủ), bot áp dụng thuật toán **Lùi bước theo hàm mũ (Exponential Backoff)**: Tự động thử lại chậm dần để tránh việc spam yêu cầu làm nghẽn hệ thống.
- Nếu các lệnh giao dịch (Orders) gửi lên sàn bị thất bại do Quá thời gian chờ (Timeout), chúng sẽ được gán cờ trạng thái Không XÁc Định (`UNCERTAIN_STATE`). Các lệnh này sẽ được Động cơ Rủi ro theo dõi gắt gao (Chống Lệnh Ma - Ghost Order Prevention) và tiến hành Thu hồi (Rollback) ngay khi API hoạt động lại để tránh bị kẹt vốn.

## 5.4. Hoàn Tác Kỹ Thuật (Emergency Rollback)

Nếu bạn phát hiện hệ thống có biểu hiện hoạt động sai lệch bất thường, cách xử lý nhanh và an toàn nhất là ngắt tiến trình ngay lập tức:
```powershell
Get-Process python | Stop-Process -Force
```
Bot VCOREX được thiết kế theo tư duy **Vô Trạng Thái (Stateless)** đối với các giao dịch, nghĩa là mọi thông tin cốt lõi vốn dĩ được lưu trữ và đối soát trực tiếp từ hệ thống của OKX. Khi khởi động lại, bot sẽ tự động tải lại danh sách vị thế (Positions) và số dư vốn (Account Equity) để tiếp tục làm việc mà không gặp hiện tượng treo dữ liệu nội bộ.