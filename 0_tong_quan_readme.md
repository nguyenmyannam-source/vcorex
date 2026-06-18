# Tổng Quan Hệ Thống VCOREX | VCOREX System Overview

**Ngày cập nhật | Last Updated:** 2026-06-13
**Phiên bản | Version:** 1.2
**Tác giả | Author:** VCOREX Team

Chào mừng bạn đến với tài liệu hướng dẫn và thông số kỹ thuật của dự án VCOREX (Bot Giao Dịch Cấp Độ Tổ Chức - Institutional Grade Trading Bot). Hệ thống tài liệu được cấu trúc hoàn toàn bằng tiếng Việt chuyên ngành kèm tiêu đề song ngữ nhằm hỗ trợ người vận hành và lập trình viên một cách tốt nhất.

## 📌 Các Tính Năng Cốt Lõi Mới Cập Nhật

1. **Động cơ Trực quan hóa (Visualization Engine):** Bot được tích hợp thư viện vẽ biểu đồ chuyên nghiệp, tự động chụp ảnh đồ thị nến, các đường EMA và chỉ báo xu hướng (ADX) ngay thời điểm vào lệnh, sau đó gửi trực tiếp qua Telegram.
2. **Cơ Chế Giải Quyết Lệnh Chốt Lời / Cắt Lỗ (TP/SL Resolver):** Hệ thống được nâng cấp để xử lý triệt để tình trạng các lệnh bị chồng chéo. Đồng thời, bot tự động gộp các hợp đồng rác (dust merge) do sai số kích thước tối thiểu của sàn, đảm bảo không lưu lại các vị thế dư thừa.

---

## 📑 Mục Lục Tài Liệu Chính | Main Documentation Outline

| Tài Liệu | Mô tả nội dung |
| :--- | :--- |
| [1. Kiến Trúc Hệ Thống (Architecture)](kien_truc_he_thong_architecture.md) | Giải thích về kiến trúc Hướng sự kiện (Event-Driven), luồng dữ liệu và các Động cơ (Engine) cốt lõi. |
| [2. Hướng Dẫn Triển Khai (Deployment)](docs/2_trien_khai_deployment.md) | Cách cài đặt, thiết lập môi trường và cấu hình máy chủ VPS/Windows từ đầu. |
| [3. Hướng Dẫn Vận Hành (Operations)](docs/3_van_hanh_operations.md) | Quản lý nhật ký hệ thống (logs), giám sát bot qua Telegram, và các thao tác bảo trì hàng ngày. |
| [4. Chiến Lược Giao Dịch (Trading Strategy)](docs/4_chien_luoc_strategy.md) | Giải thích thuật toán Giao cắt EMA (EMA Crossover), bộ lọc xu hướng ADX và cách quản trị rủi ro đa tầng. |
| [5. Phục Hồi Sự Cố (Disaster Recovery)](docs/5_phuc_hoi_recovery.md) | Cơ chế tự động khôi phục mạng lưới, hệ thống giám sát (Watchdog) và tự động thử lại (Exponential Backoff). |
| [6. Kiểm Thử Hệ Thống (System Testing)](docs/6_kiem_thu_testing.md) | Hướng dẫn khởi chạy các bài kiểm tra đơn vị (Unit Test), kiểm tra tích hợp (Integration Test). |
| [7. Bảo Mật & Chốt Chặn (Security & Guards)](docs/7_bao_mat_security.md) | Quản lý khóa kết nối API, bộ giám sát ẩn (Shadow Validator), và các biện pháp bảo vệ tài sản rủi ro. |
| [8. Tích Hợp OKX API (OKX API Integration)](docs/8_api_okx_integration.md) | Thông tin tuân thủ API v5 mới nhất của sàn OKX. |
| [9. Chuyển Máy Chủ (Server Migration)](docs/9_chuyen_may_chu_migration.md) | Hướng dẫn chi tiết từng bước an toàn khi chuyển Bot sang máy chủ VPS mới. |

---

## 🚀 Khởi Động Nhanh Trên Windows | Quick Start on Windows

### Bước 1 — Cài đặt môi trường (chỉ làm 1 lần)

```powershell
# Tạo virtual environment
python -m venv venv

# Cài đặt thư viện
venv\Scripts\pip install -r requirements.txt
```

### Bước 2 — Cấu hình API Keys (chỉ làm 1 lần)

```powershell
# Copy file cấu hình mẫu
copy .env.example .env

# Mở file .env và điền thông tin:
#   OKX_API_KEY        = <API key của bạn>
#   OKX_SECRET_KEY     = <Secret key>
#   OKX_PASSPHRASE     = <Passphrase>
#   TELEGRAM_BOT_TOKEN = <Token Telegram Bot>
#   TELEGRAM_CHAT_ID   = <Chat ID của bạn>
```

### Bước 3 — Chạy bot (hàng ngày)

#### ✅ Cách khuyến nghị — PowerShell Launcher (Auto-Restart)

```powershell
# Chuyển vào thư mục dự án trước khi chạy
cd D:\vcorex_C206_12_06_ADX_THAN_NEN_1
powershell -ExecutionPolicy Bypass -File start_bot.ps1
```

Hoặc **chuột phải** vào file `start_bot.ps1` → **Run with PowerShell**

**Tính năng của launcher:**
- Tự động kiểm tra venv, file `.env` trước khi chạy
- Tự động **restart** khi bot crash (tối đa 10 lần, delay 5 giây)
- Ghi log ra file `logs\bot_YYYY-MM-DD.log` theo từng ngày
- Hiển thị màu sắc, trạng thái rõ ràng trên terminal
- **Không restart** nếu tắt đúng cách bằng `Ctrl+C`

#### Cách thủ công (không có auto-restart)

```powershell
venv\Scripts\python.exe main.py
```

---

## 📂 Cấu Trúc File Quan Trọng | Key Files

| File | Mô tả |
| :--- | :--- |
| `start_bot.ps1` | PowerShell launcher — **dùng để chạy bot hàng ngày** |
| `main.py` | Entry point chính của bot |
| `.env` | Cấu hình API keys và thông số vận hành (**KHÔNG commit lên Git**) |
| `.env.example` | File mẫu cấu hình |
| `requirements.txt` | Danh sách thư viện Python cần cài |
| `logs\` | Thư mục chứa log file theo từng ngày |