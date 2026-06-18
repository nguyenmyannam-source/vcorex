# Kiến Trúc Hệ Thống VCOREX | VCOREX System Architecture

**Ngày cập nhật | Last Updated:** 2026-06-13
**Phiên bản | Version:** 1.1

Tài liệu này mô tả kiến trúc cốt lõi của VCOREX (Bot Giao Dịch Cấp Độ Tổ Chức - Institutional Grade Trading Bot). Hệ thống được thiết kế tối ưu hóa độ trễ thấp và khả năng mở rộng thông qua giao tiếp bất đồng bộ.

## 1. Tổng Quan Kiến Trúc | Architecture Overview

VCOREX được thiết kế theo kiến trúc **Hướng Sự Kiện (Event-Driven Architecture)**. Các thành phần giao tiếp với nhau qua cấu trúc `EventBus` độc lập. Điều này giúp hệ thống không bị thắt cổ chai hiệu suất (bottleneck) và cực kỳ dễ mở rộng khi bổ sung thêm các tính năng mới mà không phá vỡ luồng code hiện tại.

## 2. Các Luồng Sự Kiện Cốt Lõi | Core Event Flows

### 2.1. Luồng Tín Hiệu Giao Dịch | Trading Signal Flow
1. **Động cơ Tạo Tín hiệu (SignalGenerator):** Nhận dữ liệu nến (Klines) và phát tín hiệu `STRATEGY_SIGNAL_GENERATED`.
2. **Động cơ Quản Trị Rủi Ro (RiskManager):** Đánh giá rủi ro an toàn tài khoản (Bảo vệ nồng độ vốn, Ký quỹ, Trượt giá). Nếu vượt qua, phát sự kiện `RISK_SIGNAL_APPROVED`; nếu không, phát `RISK_SIGNAL_REJECTED`.
3. **Động cơ Vị Thế (PositionEngine):** Lắng nghe tín hiệu đã duyệt và gọi `OrderHandler` để xử lý mở vị thế.
4. **Động cơ Trực Quan Hóa (ChartService):** Lắng nghe sự kiện `RISK_SIGNAL_APPROVED`, song song tạo hình ảnh biểu đồ phân tích kỹ thuật (Chart) và phát tín hiệu `CHART_GENERATED`.
5. **Động cơ Xử Lý Lệnh (OrderHandler):** Xử lý logic vào lệnh thực tế qua REST/WebSocket, định tuyến lệnh Chốt lời/Cắt lỗ (TP/SL) với cơ chế Tự động thử lại (Tenacity Exponential Backoff) để vượt qua lỗi kết nối tạm thời.

### 2.2. Luồng Cập Nhật Vị Thế | Position Lifecycle Flow
- Khi WebSocket nhận thông tin khớp lệnh thực tế từ sàn, `OrderHandler` xác nhận và cập nhật trạng thái vị thế nội bộ.
- Các trạng thái vị thế tiến triển theo chu kỳ: `PENDING_SUBMIT` ➔ `OPENED` ➔ `PARTIAL_TP` ➔ `CLOSING` ➔ `CLOSED`.
- Lệnh TP/SL được giải quyết thông qua cơ chế **TP/SL Resolver**: Quản lý bộ nhớ RAM, đối chiếu vị thế với máy chủ sàn, và tự động gộp các vị thế vụn (dust positions).

## 3. Khả Năng Chịu Lỗi & Đồng Bộ Trạng Thái | Fault Tolerance & State Synchronization

- **Cơ chế Tự động Thử lại (Tenacity Exponential Backoff):** Đảm bảo các lệnh TP/SL được đặt thành công ngay cả khi API của sàn giao dịch (OKX) gặp sự cố tắc nghẽn hoặc lỗi quá tải hệ thống.
- **Xử lý Hàng Đợi Dự Phòng (Fallback Queue Worker):** Giải quyết các vị thế mồ côi (Orphaned Positions) khi không thể đặt SL/TP, tiến hành thu hồi và hoàn tác (rollback) an toàn.
- **Dịch vụ Đối Chiếu Số Liệu (ReconciliationService):** Định kỳ đồng bộ đối soát vị thế giữa cơ sở dữ liệu cục bộ và sàn giao dịch để sửa lỗi sai lệch (Cân bằng độ trôi, Vị thế ma).
- **Trình Giải Quyết Lệnh Tự Động (TPSL Resolver):** Ngăn chặn hoàn toàn lỗi chồng chéo nhiều lệnh TP/SL bằng cách phân tích tập hợp lệnh chờ (Algo Orders) hiện có và chỉ cập nhật khi cần thiết.

## 4. Giao Diện Người Dùng & Báo Cáo | UI & Reporting
- **Trình Xử Lý Telegram (TelegramHandler):** Cung cấp giao diện tương tác tức thời. Hiển thị thông báo khi tín hiệu bị từ chối, khi lệnh khớp, kèm theo hình ảnh biểu đồ kỹ thuật tự động.
- **Dịch vụ Báo Cáo (ReportService):** Trích xuất các báo cáo lợi nhuận (PnL) định kỳ hàng giờ và hàng ngày.
