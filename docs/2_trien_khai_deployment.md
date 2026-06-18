# 2. Hướng Dẫn Triển Khai | 2. Deployment Guide

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Tài liệu này hướng dẫn cách triển khai VCOREX Trading Bot lên máy chủ mới (Windows VPS/Local), thiết lập môi trường hệ thống, và cách thức xử lý các vấn đề thường gặp trong lần khởi chạy đầu tiên.

## 2.1. Yêu Cầu Hệ Thống (Prerequisites)

- **Hệ điều hành:** Windows Server 2019/2022, Windows 10/11.
- **Python:** Phiên bản 3.10 trở lên.
- **Cấu hình phần cứng tối thiểu:** 2 Core CPU, 4GB RAM, 20GB SSD.
- **Mạng lưới:** Đảm bảo máy chủ mở kết nối chiều đi (Outbound) tới cổng `443` (HTTPS) tới `https://www.okx.com`, API của Telegram, và cổng `8443` (WSS) tới `wss://wspap.okx.com:8443`.

## 2.2. Các Bước Cài Đặt Cơ Bản

### Bước 1: Sao chép mã nguồn và cấu hình

1. Chép toàn bộ mã nguồn vào thư mục làm việc (ví dụ `D:\vcorex_trading_bot`).
   *Lưu ý: Tuyệt đối không sao chép thư mục `venv` và thư mục `logs` từ máy tính khác sang để tránh xung đột môi trường cục bộ.*
2. Nhân bản tệp mẫu `.env.example` thành `.env` và điền thông tin:
   - `ENVIRONMENT=demo` (hoặc `live`)
   - Cấu hình khóa kết nối OKX: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`
   - Cấu hình Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - Thiết lập giới hạn rủi ro: `MAX_SYMBOL_CONCENTRATION=1`, `MAX_OPEN_POSITIONS=5`, v.v.

### Bước 2: Thiết lập môi trường ảo (Virtual Environment)

**Mở cửa sổ dòng lệnh PowerShell (Chạy dưới quyền Quản trị viên - Run as Administrator):**
```powershell
cd D:\vcorex_trading_bot
py -3 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```
*(Ghi chú: Quá trình cài đặt `requirements.txt` sẽ tự động tải thư viện `plotly` và `kaleido` để phục vụ chức năng tự động xuất ảnh đồ thị qua Telegram).*

## 2.3. Khởi Động Lần Đầu Và Giám Sát (First Run Guide)

### Bước 1: Khởi chạy Bot
Tại cửa sổ dòng lệnh đã kích hoạt môi trường ảo `venv`, thực thi lệnh:
```bash
python main.py
```
*(Lưu ý: Không đóng cửa sổ dòng lệnh này nếu bạn chạy trực tiếp, nếu đóng bot sẽ tự động tắt. Bạn có thể thu nhỏ cửa sổ để bot chạy ngầm).*

### Bước 2: Quan sát quá trình khởi động (3-5 phút đầu)

Bot sẽ thực hiện quy trình sau:
1. ✅ Khởi tạo hệ thống động cơ và kết nối OKX.
2. ⚠️ Có thể hiển thị một số cảnh báo an toàn (Warnings).
3. ✅ Khôi phục dữ liệu vị thế (Atomic Resync) từ sàn OKX về Cơ sở dữ liệu nội bộ.
4. ✅ Tải dữ liệu thị trường trong quá khứ (Market Data Bootstrap) mất khoảng 3-5 phút.
5. ✅ Sẵn sàng giao dịch khi xuất hiện dòng thông báo: `Strategy engine started`.

### Các cảnh báo thông thường (Normal Warnings) hay gặp:

- **Thay đổi định dạng Demo UID:** Sàn OKX hiện tại trả về UID dạng số cho tài khoản Demo (không còn hậu tố `-demo`). Bot sẽ hiện cảnh báo nhắc nhở bạn xác nhận lại API Key. Đây là hiện tượng bình thường.
- **Shadow Validator - Sai lệch vị thế:** Cảnh báo có sự bất đồng bộ vị thế giữa cơ sở dữ liệu nội bộ và sàn OKX. Đây là bình thường trong lần chạy đầu tiên, ngay sau đó bot sẽ tự động thực hiện **Đồng bộ hóa dữ liệu tuyệt đối (Atomic Resync)** để sửa lỗi, và cảnh báo sẽ biến mất.
- **Không tìm thấy Redis:** Nếu bạn không dùng Redis, bot sẽ tự động chuyển về dùng bộ đệm nội bộ `InProcessEventBus` (Hoàn toàn bình thường cho việc chạy trên máy chủ đơn lẻ).

## 2.4. Kiểm Tra Tình Trạng Bot (Monitoring)

1. **Kiểm tra thông qua Telegram:** 
   Gửi lệnh `/status` tới bot Telegram, bot sẽ trả về trạng thái hoạt động trực tiếp (Thời gian chạy - Uptime, Vị thế đang mở, Trạng thái kết nối).
2. **Kiểm tra Nhật ký hệ thống (Logs):** 
   Theo dõi tệp `logs/vcorex.log` để xem tiến trình. Nếu thấy `INFO | services.strategy_engine:_run:120 - Strategy engine started` nghĩa là hệ thống đã hoàn toàn sẵn sàng.
3. **Tắt hệ thống an toàn (Graceful Shutdown):**
   Nhấn `Ctrl + C` tại cửa sổ dòng lệnh đang khởi chạy bot. Bot sẽ thực hiện quy trình Tắt Hệ Thống An Toàn trong 3-5 giây (Lưu lại dữ liệu, hủy tất cả các lệnh chờ, đóng kết nối an toàn).
