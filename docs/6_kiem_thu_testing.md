# 6. Kiểm Thử Hệ Thống | 6. System Testing

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Dự án sở hữu một bộ Kiểm thử Đơn vị (Unit Test) và Kiểm thử Tích hợp (Integration Test) ở cấp độ chuyên sâu, đáp ứng tiêu chuẩn của các tổ chức tài chính, và được phát triển trên nền tảng framework `pytest`.

## 6.1. Cấu Trúc Thư Mục Kiểm Thử (Tests)
```text
tests/
├── unit/            ← Kiểm thử từng hàm, từng lớp riêng lẻ (Sử dụng kỹ thuật Mocking giả lập OKX API, giả lập Telegram).
├── integration/     ← Kiểm thử chuỗi luồng giao tiếp logic giữa nhiều thành phần (Động cơ dữ liệu ➔ Chiến lược ➔ Đặt lệnh).
├── institutional/   ← Kiểm thử rủi ro cấp độ hệ thống lớn (Mô phỏng Monte Carlo, Khả năng chịu trượt giá).
├── performance/     ← Kiểm thử hiệu suất, đo đếm độ trễ (Latency) và khả năng chống nghẽn API.
└── security/        ← Kiểm thử bảo mật vào lệnh, kiểm tra hàng rào Ngăn Chặn Lệnh Ma (Ghost Order Prevention).
```

## 6.2. Khởi Chạy Các Bài Kiểm Thử

Mở cửa sổ dòng lệnh (Terminal/PowerShell) tại thư mục gốc của dự án:
```powershell
.\venv\Scripts\pytest tests/ -v
```

Để chạy các nhóm kiểm thử cụ thể:
```powershell
# Chỉ chạy bộ kiểm thử Đơn vị (Unit tests)
.\venv\Scripts\pytest tests/unit/ -v

# Chỉ chạy các bài kiểm thử liên quan đến Telegram và Trực quan hóa
.\venv\Scripts\pytest tests/unit/telegram/ -v

# Chỉ chạy bài kiểm thử rủi ro hệ thống mô phỏng Monte Carlo
.\venv\Scripts\pytest tests/institutional/test_monte_carlo_risk.py -v
```

## 6.3. Tiêu Chuẩn Soạn Thảo Bài Kiểm Thử Mới
- **Hỗ trợ Bất đồng bộ:** Luôn sử dụng decorator `@pytest.mark.asyncio` khi khai báo các hàm kiểm thử có chứa `await`.
- **Tuyệt đối không kết nối mạng thật ở cấp độ Unit Test:** Mọi thử nghiệm trong thư mục `unit/` phải sử dụng `AsyncMock` hoặc các lớp cấu trúc giả lập (`Dummy`) có sẵn trong mã nguồn để giả lập luồng dữ liệu trả về từ sàn OKX.
- **Thử nghiệm các trường hợp Cực Đoan (Edge cases):** Cần đảm bảo viết thêm các trường hợp kiểm thử cho những tình huống dị thường trên thị trường (Ví dụ: Biến động giá nhảy vọt, API sàn giao dịch trả về mã lỗi 429 quá tải hoặc mã 500 sập máy chủ).