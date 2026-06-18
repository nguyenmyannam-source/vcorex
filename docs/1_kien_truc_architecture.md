# 1. Kiến Trúc Hệ Thống | 1. System Architecture

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Dự án VCOREX Trading Bot được thiết kế theo kiến trúc Hướng Sự Kiện (Event-Driven Architecture) với trọng tâm là độ trễ thấp (Low Latency) và an toàn tài khoản (Institutional-grade Security).

## 1.1. Các Thành Phần Cốt Lõi (Core Components)

Hệ thống được chia thành 5 Động cơ chính, giao tiếp với nhau thông qua Bộ truyền sự kiện trung tâm (`EventBus`):

1. **Động cơ Dữ Liệu Thị Trường (`services/market_data_engine.py`)**
   - Chịu trách nhiệm duy trì kết nối WebSocket liên tục với sàn OKX (`/ws/v5/business`).
   - Quản lý bộ đệm nến (`CandleBuffer`) cho hơn 20 đồng tiền mã hóa trên nhiều khung thời gian (5 phút, 15 phút, 1 giờ...).
   - Tự động tính toán các chỉ báo kỹ thuật (EMA) qua `IndicatorPipeline`.
   - **Tính năng đặc biệt:** Có bộ giám sát (Watchdog) tự động khôi phục kết nối khi sàn mất tín hiệu (Giám sát Sức khỏe Luồng Dữ Liệu), và dự phòng (Fallback) sang REST API nếu WebSocket bị treo.

2. **Động cơ Chiến Lược (`services/strategies/strategy_engine.py`)**
   - Nhận tín hiệu (Event) từ Động cơ Dữ Liệu Thị Trường mỗi khi nến đóng.
   - Quản lý và thực thi đa chiến lược (Multi-strategy).
   - Kiểm tra các điều kiện vào lệnh (Ví dụ: Giao cắt EMA) và tạo Tín hiệu (`Signal`).
   - Chống hiện tượng vẽ lại biểu đồ (repainting) bằng cách lấy nến `[-2]` (nến đã đóng hoàn toàn) thay vì nến đang hình thành.

3. **Động cơ Rủi Ro & Vị Thế (`domain/risk/risk_manager.py`, `services/position/order_handler.py`)**
   - Nhận Tín hiệu và đánh giá rủi ro theo thời gian thực.
   - Kiểm tra số dư khả dụng tổng thể trên tài khoản.
   - Tính toán khối lượng vào lệnh tự động dựa trên biên độ Cắt Lỗ và tỷ lệ rủi ro phần trăm tài khoản.
   - Ngăn chặn việc vào lệnh chồng chéo (Anti-overlap) và giới hạn số lệnh tối đa đồng thời. Có tích hợp bộ cảnh vệ tập trung vốn (Symbol Concentration Guard).
   - Xử lý mượt mà và triệt để các lệnh chồng chéo, tự động thu dọn và gộp các hợp đồng vụn (dust merge) do sai số kích thước tối thiểu của sàn.

4. **Động cơ Giao Tiếp Sàn OKX (`infrastructure/exchange/okx_exchange.py`)**
   - Lớp giao tiếp trực tiếp với sàn OKX thông qua REST API và WebSocket.
   - Xử lý xác thực, tự động tính toán bù trừ độ lệch thời gian (Time Sync offset) để chống lỗi mốc thời gian (Timestamp mismatch).
   - Quản lý giới hạn tốc độ (Rate Limit) bằng thuật toán Hộp Thẻ Phạt (TokenBucketLimiter) và Cờ Hiệu (Semaphore) giúp hệ thống không bao giờ bị OKX khóa IP dù chạy hàng chục đồng tiền đồng thời.

5. **Động cơ Trực Quan Hóa (`services/visualization/chart_service.py`)**
   - Lắng nghe các tín hiệu Giao Dịch được Chấp Thuận.
   - Tự động kết xuất đồ thị biểu đồ nến thông qua thư viện kỹ thuật `matplotlib` và `mplfinance`. Tích hợp đầy đủ các lớp phủ như EMA, chỉ báo xung lượng xu hướng ADX, và đánh dấu chính xác điểm vào lệnh dự kiến.
   - Hoạt động bất đồng bộ ở chế độ nền (headless), đảm bảo không gây gián đoạn hay làm chậm luồng xử lý sự kiện chính. Sau đó phát sự kiện chuyển hình ảnh tới Trình xử lý Telegram.

## 1.2. Luồng Xử Lý Sự Kiện

1. `OKX WebSocket` ➔ Gửi nến mới (Sự kiện: `MARKET_WS_CANDLE`).
2. `Động cơ Dữ Liệu Thị Trường` ➔ Cập nhật bộ đệm, tính toán EMA, phát tín hiệu nến hoàn thành.
3. `Động cơ Chiến Lược` ➔ Chạy logic chiến lược, nếu thỏa mãn điều kiện sẽ phát tín hiệu `STRATEGY_SIGNAL_GENERATED`.
4. `Động cơ Rủi Ro` ➔ Kiểm duyệt tín hiệu, nếu an toàn, tính toán quy mô lệnh và phát tín hiệu hợp lệ (`RISK_SIGNAL_APPROVED`).
5. `Động cơ Trực Quan Hóa` ➔ Nhận tín hiệu hợp lệ, xử lý vẽ biểu đồ và chuyển giao ảnh đồ thị.
6. `Xử Lý Lệnh` ➔ Gửi yêu cầu REST API tới OKX để thực thi lệnh (Thị trường/Giới hạn).
7. `Sàn OKX` ➔ Khớp lệnh và trả về thông tin qua WebSocket.
8. `Động cơ Vị Thế` ➔ Nhận cập nhật trạng thái vị thế thực tế, lưu nhật ký hệ thống và gửi thông báo tổng hợp tới Telegram.
