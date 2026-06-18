# 9\. Hướng Dẫn Chuyển Máy Chủ | 9. Server Migration Guide

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

\---

Tài liệu này hướng dẫn quy trình chuyển giao hệ thống **Bot Giao Dịch VCOREX (Institutional Trading Bot)** sang máy chủ mới (Windows VPS/Local) mà vẫn đảm bảo tính liên tục của dữ liệu, giữ nguyên các vị thế đang mở và duy trì khả năng vận hành 24/7. Đây là tài liệu đầy đủ và chi tiết nhất để thay thế cho mọi phiên bản cũ.

\---

## 📅 QUY TRÌNH TỔNG QUAN

1. **Dừng hệ thống \& Sao lưu** dữ liệu cũ.
2. **Cài đặt môi trường** trên máy chủ mới.
3. **Triển khai mã nguồn** \& Khôi phục dữ liệu (Xóa môi trường ảo cũ, xây dựng môi trường ảo mới).
4. **Kiểm thử hệ thống** (Kiểm thử tự động \& Kết nối API).
5. **Kích hoạt vận hành** 24/7 \& Giám sát.

\---

## 🛠️ CHI TIẾT CÁC BƯỚC THỰC HIỆN

### Bước 1: Dừng Hệ Thống \& Sao Lưu Dữ Liệu (Trên Máy Cũ)

1. **Dừng hệ thống an toàn (Graceful Shutdown):**

   * Mở cửa sổ dòng lệnh đang chạy bot, nhấn `Ctrl + C` và đợi 3-5 giây để hệ thống ghi lưu cơ sở dữ liệu.
   * **Giữ vị thế khi chuyển máy chủ:** Trong tệp `.env`, đổi biến `SHUTDOWN\_LIQUIDATE\_ON\_EXIT=false` trước khi dừng bot. Nếu để mặc định `true`, bot sẽ đóng toàn bộ vị thế theo giá thị trường khi tắt.
   * **Xử lý nếu bị kẹt tiến trình (Lỗi không nén được thư mục hoặc Từ chối truy cập):**
Khởi chạy lệnh sau trong PowerShell để tắt hoàn toàn các tiến trình Python đang chạy ngầm dưới quyền Quản trị viên:

```powershell
     Start-Process powershell -Verb RunAs -ArgumentList "taskkill /F /IM python.exe"
     ```

2. **Nén và sao lưu thư mục dự án:**

   * **QUAN TRỌNG:** **TUYỆT ĐỐI KHÔNG** nén thư mục môi trường ảo `venv` và thư mục nhật ký `logs`. Chúng chứa hàng chục nghìn tệp tin nhỏ sẽ gây lỗi nén, làm dung lượng tệp nén quá nặng. Đặc biệt, môi trường `venv` sao chép sang máy khác sẽ gây ra lỗi đường dẫn tuyệt đối (Absolute Path Error) khiến bot không thể chạy.
   * **Cách nén tự động bằng PowerShell:**
Chạy lệnh sau tại thư mục gốc của dự án để tự động tạo tệp nén `vcorex\_bot.zip` sạch sẽ tại thư mục cha:

```powershell
     Get-ChildItem -Exclude "venv", "logs", "\*.zip" | Compress-Archive -DestinationPath "..\\vcorex\_bot.zip" -Force
     ```

   * **Thủ công:** Dùng WinRAR/7-Zip nén toàn bộ thư mục gốc nhưng nhớ **bỏ chọn/xóa** thư mục `venv` và `logs` trước khi nén.

\---

### Bước 2: Chuẩn Bị Môi Trường (Trên Máy Mới)

1. **Cài đặt Python 3.10+:**

   * **Windows:** Tải bộ cài đặt `.exe` chính thức, **BẮT BUỘC** tích chọn ô **`Add Python to PATH`** ở màn hình cài đặt đầu tiên.
2. **Cấu hình mạng lưới (Tường lửa - Firewall):**
Đảm bảo máy chủ mới mở kết nối chiều đi (Outbound) tới:

   * Cổng `443` (HTTPS) tới `https://www.okx.com` và API của Telegram.
   * Cổng `8443` (WSS) tới `wss://wspap.okx.com:8443` (Cổng luồng dữ liệu riêng của OKX WebSockets).

\---

### Bước 3: Sao Chép Mã Nguồn \& Khôi Phục Dữ Liệu

1. **Triển khai mã nguồn:**

   * Giải nén tệp `vcorex\_bot.zip` sang máy chủ mới.
   * Đảm bảo tệp cấu hình ẩn `.env` đã được sao chép thành công.
   * Đảm bảo tệp cơ sở dữ liệu `data/vcorex.db` đã có mặt.
   * *(Lưu ý: Nếu địa chỉ IP của VPS/Máy chủ mới bị thay đổi, hãy đăng nhập OKX ➔ Quản lý API ➔ Cập nhật Danh sách IP Trắng (IP Whitelist)).*
2. **Thiết lập môi trường ảo (`venv`) mới hoàn toàn:**

   Để tránh tuyệt đối các lỗi sai lệch đường dẫn, hãy thực hiện quy trình xây dựng lại từ đầu (Clean Rebuild) bằng PowerShell (Chạy dưới quyền Quản trị viên):

   ```powershell
   # 1. Hủy kích hoạt venv cũ và XÓA SẠCH thư mục venv cũ (đề phòng copy nhầm)
   deactivate
   Remove-Item -Recurse -Force .\\venv -ErrorAction SilentlyContinue

   # 2. Khởi tạo venv mới TỪ ĐẦU
   py -3 -m venv venv hoặc \\\& "D:\\\\Python\\\\python.exe" -m venv venv & "E:\python3.12\python.exe" -m venv venv

   # 3. Kích hoạt venv
   .\\venv\\Scripts\\Activate.ps1

   # 4. Nâng cấp bộ cài đặt gói (pip)
   python -m pip install --upgrade pip

   # 5. Cài đặt toàn bộ thư viện cần thiết
   # - Dành cho môi trường thực tế: python -m pip install -r requirements.txt
   # - Dành cho kiểm thử và phát triển: python -m pip install -r requirements-dev.txt
   python -m pip install -r requirements-dev.txt
   ```

   *(Nếu gặp lỗi đỏ `cannot be loaded because running scripts is disabled`, hãy chạy lệnh: `Set-ExecutionPolicy Unrestricted -Scope CurrentUser` rồi thực hiện lại lệnh số 3).*

   \---

   ### Bước 4: Kiểm Thử Hệ Thống (Testing Phase)

   Trước khi chạy bot thực tế với tiền của bạn, hãy chạy các kịch bản kiểm thử tự động để đảm bảo máy mới hoạt động 100% trơn tru:

   *(Lưu ý: Nếu đã cài đặt `requirements-dev.txt` ở Bước 3, các thư viện kiểm thử đã có sẵn, có thể bỏ qua dòng lệnh số 1)*

   ```powershell
# 1. (Tùy chọn) Cài thêm thư viện kiểm thử:
python -m pip install pytest pytest-asyncio hypothesis pytest-benchmark

# 2. Khởi chạy toàn bộ Kiểm thử Đơn vị \& Kiểm thử Tích hợp
python -m pytest tests/ -v

# (Hoặc) Chạy riêng bài kiểm thử khôi phục vị thế quan trọng nhất:
python -m pytest tests/integration/test\_exchange\_mirror\_atomic.py -v

# 3. (Tùy chọn) Khởi chạy Kịch bản Kiểm tra Sức khỏe Hệ thống:
python health\_check.py
```

   **Chỉ khi cửa sổ dòng lệnh trả về dòng chữ `Passed` màu xanh lá cây thì bạn mới chuyển sang Bước 5.**

   \---

   ### Bước 5: Kích Hoạt Vận Hành Chính Thức \& Xử Lý Cảnh Báo

   Mở terminal/CMD tại thư mục bot và chạy lệnh sau để khởi động:

   ```bash
python main.py
```

   *(Nếu đóng cửa sổ này, bot sẽ tắt. Bạn có thể thu nhỏ cửa sổ để bot chạy ngầm).*

   #### ⚠️ QUAN TRỌNG: Các Cảnh Báo Có Thể Xuất Hiện Lần Đầu Tiên (RẤT BÌNH THƯỜNG)

   Khi vừa chuyển qua máy mới, bot sẽ in ra một vài thông báo cảnh báo (WARNING). Đừng lo lắng, đây là thiết kế an toàn của hệ thống:

   **1. Cảnh báo Định dạng Demo UID (OKX API Changes 2026):**
Kể từ 2026, OKX đổi định dạng UID của tài khoản Demo từ chuỗi có chữ `-demo` thành chuỗi số thuần túy (Ví dụ: `682651107994596407`).
Bạn sẽ thấy nhật ký: `WARNING: Demo mode is enabled but the OKX account UID does not contain a clear demo marker.`
➔ **Bỏ qua**, đây là thông báo bình thường để nhắc nhở bạn.

   **2. Cảnh báo Lệch Vị Thế (Shadow Diff):**
Bạn sẽ thấy nhật ký: `\[SHADOW DIFF] Lệch Bóng: Sàn có vị thế... nhưng Cơ sở dữ liệu Nội bộ không có!`
Nguyên nhân là do máy mới chưa kịp tải đồng bộ với máy chủ OKX.
➔ **Hệ thống tự động khắc phục:** Vài giây sau bạn sẽ thấy thông báo `\[MIRROR] Atomic Resync Success!`. Bot đã kéo dữ liệu từ sàn về và sửa lỗi này hoàn toàn tự động.

   **3. Hoàn tất quá trình chuyển giao máy chủ:**
Khi bạn nhìn thấy dòng chữ **`Strategy engine started, waiting for entry signals...`**, xin chúc mừng, quá trình chuyển đổi đã THÀNH CÔNG RỰC RỠ!

   \---

   ### ✅ Bước 6: Danh Sách Kiểm Tra Sau Chuyển Đổi (Verification Checklist)

   Sau khi bot khởi động thành công, hãy kiểm tra các mục sau để đảm bảo hệ thống hoạt động hoàn hảo:

   #### **Kiểm Tra Cơ Bản (5 phút)**

* \[ ] Bot in ra dòng thông báo `Strategy engine started, waiting for entry signals...`
* \[ ] Không có lỗi nghiêm trọng (ERROR) xuất hiện (chỉ INFO/WARNING là bình thường).
* \[ ] Bot Telegram phản hồi mượt mà với lệnh `/dashboard` hoặc `/status`.
* \[ ] Kết nối OKX WebSocket hiển thị trạng thái "Đã kết nối" (Connected).

  #### **Kiểm Tra Đồng Bộ Dữ Liệu (10 phút)**

* \[ ] Nhật ký hiển thị `\[MIRROR] Atomic Resync Success!` (Đã đồng bộ vị thế từ OKX).
* \[ ] Nếu có vị thế cũ, Bảng điều khiển Telegram hiển thị đúng số lượng vị thế.
* \[ ] Tệp Cơ sở dữ liệu `data/vcorex.db` có dung lượng > 0 KB.
* \[ ] Không còn thấy cảnh báo `\[SHADOW DIFF]` sau 2-3 phút hoạt động đầu tiên.

  #### **Kiểm Tra Tính Năng (15 phút)**

* \[ ] Hệ thống tự động đẩy Biểu đồ Trực quan hóa nếu có tín hiệu vào lệnh mới.
* \[ ] Các nút điều khiển trên Telegram hoạt động bình thường (Khởi động/Dừng/Điều chỉnh Radar).
* \[ ] Tệp `logs/trades.jsonl` ghi nhận lịch sử lệnh chuẩn xác.

  #### **Kiểm Tra Hiệu Suất (Tùy chọn)**

* \[ ] Khởi chạy lại hệ thống kiểm thử: `python -m pytest tests/ -v` (Kết quả Passed toàn bộ).
* \[ ] Mức tiêu thụ RAM duy trì ổn định dưới 500MB.
* \[ ] Mức sử dụng CPU nhỏ hơn 20% khi ở chế độ chờ (Idle).

  \---

  ### 📞 Xử Lý Sự Cố Bất Thường

  Nếu hệ thống gặp lỗi phát sinh không thể tự xử lý:

1. Rà soát lại Lịch sử thực thi trong thư mục `logs/`.
2. Kiểm tra tệp `logs/errors.log` để xem chi tiết lỗi rò rỉ.
3. Chạy bài kiểm thử trọng yếu: `python -m pytest tests/integration/test\_exchange\_mirror\_atomic.py -v`.

