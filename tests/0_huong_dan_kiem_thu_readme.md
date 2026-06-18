# Hướng Dẫn Kiểm Thử | Testing Guide

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Tài liệu này cung cấp hướng dẫn chi tiết cách khởi chạy kiểm thử tự động, cấu trúc các tệp kiểm thử và các câu lệnh hữu ích phục vụ Tích hợp Liên tục/Phân phối Liên tục (CI/CD) hoặc kiểm tra độ ổn định của hệ thống trước khi triển khai lên VPS môi trường Thực tế (Production).

---

## 🧪 1. Cấu Trúc Bộ Kiểm Thử (Testing Suite Structure)

Thư mục `tests/` được tổ chức khoa học nhằm bao phủ toàn diện các hoạt động của bot:

* **`tests/unit/`**: Kiểm thử đơn vị, cô lập từng thành phần nghiệp vụ cốt lõi. Sử dụng cơ chế Giả lập hoàn toàn (Mocking) để tránh gọi REST API ra bên ngoài:
  * Kiểm thử thuật toán Chốt lời / Cắt lỗ (TP/SL) trong `test_strategy.py`.
  * Kiểm thử bộ chốt chặn quản trị rủi ro độc lập trong `test_risk_manager.py`.
  * Kiểm thử định dạng bản tin Telegram trong `test_formatters.py`.
  * Kiểm thử các cơ chế bảo vệ mới nhất (`tests/unit/test_phase3_hardening.py`):
    * **Bộ Bảo Vệ Tính Đồng Nhất (Idempotency Guard)**: Lọc trùng sự kiện bằng mã băm MD5 và phiên bản thời gian `uTime`.
    * **Hẹn giờ Hủy Lệnh Rác (Transient Order Timeout)**: Dọn dẹp lệnh không phản hồi sau 5 giây.
    * **Kho Lưu Ngữ Cảnh Giao Dịch (TradeJournalContextStore)**: Cô lập vòng đời ngữ cảnh giao dịch theo từng mã tài sản (symbol).
* **`tests/integration/`**: Kiểm thử tích hợp để xác nhận sự phối hợp hoạt động đa tầng qua `EventBus`:
  * Gửi tín hiệu giả lập ➔ Động cơ Rủi ro phê duyệt ➔ Động cơ Vị thế mở lệnh ➔ Ghi nhận SQLite.
  * Kiểm thử tích hợp Bảng điều khiển Telegram và tính năng Đặt lại bộ đếm dữ liệu.
* **`tests/performance/`**: Kiểm thử hiệu năng hệ thống nhằm đo lường độ trễ xử lý (Processing Latency) dưới áp lực tải dữ liệu nến WebSocket đồng thời của hơn 20 cặp tiền mã hóa.
* **`tests/institutional/`**: Các kịch bản kiểm thử nâng cao đạt chuẩn định chế (Chaos Engineering):
  * **Sự Cố Cơ Sở Dữ Liệu (Database Failure)**: Khả năng phục hồi dữ liệu trong bộ nhớ đệm RAM khi SQLite bị đầy hoặc bị khóa cứng.
  * **Dữ Liệu WebSocket Cũ (Stale WS Data)**: Tự động phát hiện dữ liệu luồng WebSocket bị hết hạn (vượt TTL) và tự động chuyển đổi sang REST API để tính PnL liên tục.
  * **Đồng Bộ Vị Thế Ma (Ghost Position Resync)**: Mô phỏng sự cố gián đoạn mạng và khôi phục đồng bộ hóa các vị thế được tạo thủ công thông qua quy trình **Đồng bộ hóa Tuyệt đối (Atomic Resync)**.
* **`tests/professional/`**: Bộ kiểm thử chuyên nghiệp cấp Tổ chức, bao gồm:
  * Kịch bản thế giới thực (Real-world Scenarios).
  * Kiểm thử Hỗn loạn và Sức chịu đựng (Chaos & Stress Testing).
  * Kiểm thử độ chính xác thông báo Telegram (Telegram Professional).
  * Kiểm thử tuân thủ tiêu chuẩn P1/P2/P3 (Compliance Validation).

---

## 🚀 2. Các Câu Lệnh Khởi Chạy Kiểm Thử (Pytest)

Hệ thống sử dụng thư viện `pytest` và `pytest-asyncio` được cấu hình tự động thông qua tệp `pytest.ini`.

Trước khi khởi chạy, hãy chắc chắn môi trường ảo (venv) đã được kích hoạt:

### Khởi chạy toàn bộ bộ kiểm thử:
```bash
python -m pytest
```

### Chỉ chạy riêng nhóm bài Kiểm thử Đơn vị (Unit Test):
```bash
python -m pytest tests/unit/ -v
```

### Chỉ chạy riêng một tệp kiểm thử cụ thể:
```bash
python -m pytest tests/unit/test_phase3_hardening.py -v
```

### Chạy bộ kiểm thử Chuyên nghiệp (Professional Suite) đầy đủ:
```bash
python -m pytest tests/professional/ -v
```

### Chạy với Báo cáo Phủ sóng mã (Coverage Report):
```bash
python -m pytest tests/ -v --cov --cov-report=html
```

---

## 📋 3. Tiêu Chí Kết Quả Chấp Nhận

Hệ thống kiểm thử được xem là **ĐẠT** khi:
- Tất cả 212+ bài kiểm thử có kết quả `PASSED` (màu xanh lá cây).
- Không có bất kỳ bài nào có kết quả `ERROR` (màu đỏ). Các kết quả `SKIPPED` là chấp nhận được.
- Thời gian hoàn thành toàn bộ bộ kiểm thử dưới 120 giây.

---

## 💡 4. Lưu Ý Quan Trọng Khi Viết Bài Kiểm Thử Mới
- **Hỗ trợ Bất đồng bộ:** Luôn sử dụng `@pytest.mark.asyncio` cho các hàm có `await`.
- **Tuyệt đối không gọi mạng thật ở `unit/`:** Luôn dùng `AsyncMock` hoặc lớp `DummyClient` có sẵn.
- **Kiểm thử các trường hợp Ngoại lệ (Edge cases):** Bổ sung kịch bản API sàn OKX trả về mã lỗi `429` (Quá tải) hoặc `500` (Sập máy chủ).
