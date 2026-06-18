# 8. Tích Hợp OKX API v5 | 8. OKX API v5 Integration

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Tài liệu này tổng hợp các thay đổi và cập nhật quan trọng của nền tảng OKX API v5 vào năm 2026 và cách thức VCOREX Bot tuân thủ các quy chuẩn này, đảm bảo hệ thống vận hành hoàn hảo trong cả môi trường Giao dịch Thực (Production) và Giao dịch Mô phỏng (Demo).

## 8.1. Các Thay Đổi Quan Trọng Từ Sàn OKX (Tháng 5/2026)

### 1. Thay đổi định dạng ID Người dùng (UID) hệ Demo
Kể từ tháng 5 năm 2026, OKX đã thay đổi hoàn toàn định dạng UID dành cho tài khoản Demo:
- **Trước đây:** UID tài khoản Demo luôn có hậu tố `-demo` (ví dụ: `1234567-demo`). Hệ thống bot thường dựa vào đặc điểm này để phân loại dữ liệu an toàn.
- **Hiện tại:** OKX trả về chuỗi UID dạng số học thuần túy (ví dụ: `682651107994596407`), không còn đuôi `-demo` như trước.
- **Giải pháp của VCOREX:** Bot đã được nâng cấp cơ chế Xác thực Đa tín hiệu (Multi-signal Validation). Thay vì chỉ kiểm tra chuỗi UID, hệ thống sử dụng kết hợp cổng giao tiếp (endpoint) chuyên biệt và đánh giá Cờ hiệu mô phỏng `x-simulated-trading` trong Phần đầu (Headers) của yêu cầu. Nhờ đó bot vẫn nhận diện chính xác 100% môi trường làm việc.

### 2. Yêu cầu bắt buộc tham số `ordType` đối với Lệnh Thuật Toán
Khi thực hiện truy vấn Danh sách lệnh chờ thuật toán (Pending Algo Orders), OKX yêu cầu bổ sung bắt buộc tham số loại lệnh `ordType`.
- **Lỗi phổ biến (Mã 51000):** Sàn trả về lỗi `Parameter ordType error` làm sập trình xử lý.
- **Giải pháp:** Lõi VCOREX đã được cập nhật truyền cứng tham số `ordType="conditional"` trên tất cả các lệnh gọi tới cổng `/api/v5/trade/orders-algo-pending`, đảm bảo truy xuất dữ liệu thông suốt.

## 8.2. Tiêu Chuẩn Tuân Thủ Giao Thức API v5

Hệ thống Bot đã trải qua quá trình kiểm toán (Audit) toàn diện và được xác nhận tuân thủ 100% các tiêu chuẩn kỹ thuật khắt khe của OKX API v5:

1. **Xác thực mã hóa WebSocket:** Áp dụng thuật toán chữ ký `HMAC-SHA256` chuẩn xác và bảo mật.
2. **Đường dẫn Kết nối Tối ưu (URL):**
   - Môi trường Mô phỏng (Demo): `wss://wspap.okx.com:8443/ws/v5`
   - Môi trường Thực tế (Production): `wss://ws.okx.com:8443/ws/v5`
3. **Quản trị Giới hạn tốc độ (Rate Limit):**
   - Dữ liệu Công khai (Public): Giới hạn dưới 20 yêu cầu / giây.
   - Dữ liệu Cá nhân (Private): Giới hạn dưới 60 yêu cầu / giây.
   - Toàn hệ thống được kiểm soát theo hàng đợi thời gian thực thông qua công cụ Hộp thẻ phạt (`TokenBucketLimiter`), cam kết không bao giờ vượt ngưỡng Chặn Toàn Cầu (Global Rate Limit) của OKX, tránh việc bị sàn khóa tạm thời.
4. **Cấu trúc Lệnh Thuật Toán:** Sử dụng chính xác tham số Phân bổ vốn `tdMode` (cross/isolated), Kích thước lô `sz`, và Cờ hiệu giảm vị thế `reduceOnly` khi đặt các lệnh tự động Cắt lỗ / Chốt lời.

## 8.3. Cấu Hình URL Khuyến Nghị
Nhà điều hành cần đảm bảo cấu hình đúng biến môi trường `.env` theo khuyến nghị của OKX:
- URL Căn bản (Base URL) luôn là: `https://openapi.okx.com` (Tuyệt đối không sử dụng `https://www.okx.com` để gọi API giao dịch tự động nhằm tối ưu hóa đường truyền máy chủ).

## 8.4. Xử Lý Các Cảnh Báo API Liên Quan

**1. Lệch Bóng Vị Thế (Shadow Diff)**
Đây hoàn toàn không phải là lỗi API. Trong lúc vận hành, hệ thống nhật ký đôi lúc hiển thị: `[SHADOW DIFF] Lệch Bóng: Sàn có vị thế...`. Điều này xảy ra tự nhiên do độ trễ đồng bộ mạng lưới internet. Bot sẽ tự động xử lý ngay lập tức thông qua cơ chế **Đồng bộ Dữ liệu Tuyệt đối (Atomic Resync)**. Không yêu cầu bất kỳ can thiệp thủ công nào.

**2. Trôi Số Dư (Balance Drift)**
Ví dụ log: `Reconciliation anomalies detected: {'balance_drift': [{'asset': 'USDT', 'drift': 105.01}]}`
Sự chênh lệch số dư siêu nhỏ (thường < 0.2%) là hiện tượng vật lý bình thường do:
- Hệ thống trừ Phí giao dịch chưa thanh toán (Unrealized Fees).
- Sàn đóng băng mức Ký quỹ cho các lệnh chờ đang lơ lửng chưa khớp.
- Thay đổi tỷ lệ Phí tài trợ (Funding Rate) theo thời gian thực (đối với hợp đồng Tương lai/Swap).
