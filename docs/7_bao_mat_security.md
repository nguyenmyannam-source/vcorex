# 7. Bảo Mật & Chốt Chặn | 7. Security & Safeguards

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.1
**Tác giả:** VCOREX Team

---

Hệ thống giao dịch tự động cấp độ Tổ chức (Institutional) đòi hỏi sự kiểm soát an ninh cực kỳ chặt chẽ nhằm bảo vệ tài sản, ngăn chặn rò rỉ Khóa truy cập (API Keys) và hạn chế tổn thất tối đa khi hệ thống bị xáo trộn hoặc hoạt động sai logic do tác nhân bên ngoài.

Tài liệu này trình bày chi tiết các lớp bảo mật được tích hợp sẵn sâu bên trong cấu hình và mã nguồn của VCOREX.

---

## 🔑 1. Quản Lý Khóa Kết Nối An Toàn (API Key Hygiene)

* **Tách Biệt Biến Môi Trường**: Tuyệt đối **không gán cứng (hardcode)** thông tin khóa bảo mật (API Key, Secret, Passphrase) vào mã nguồn. Tất cả phải được lưu trong tệp ẩn `.env` và được nạp an toàn thông qua cấu trúc Pydantic Settings (`core/config/settings.py`).
* **Danh Sách Trắng IP (IP Whitelisting)**: Khi tạo Khóa API trên nền tảng OKX, bắt buộc phải bật tính năng giới hạn địa chỉ IP và điền chính xác IP tĩnh của máy chủ VPS chạy bot. Mọi yêu cầu gửi lệnh từ một IP lạ sẽ bị sàn từ chối ngay lập tức, vô hiệu hóa nguy cơ hacker lấy cắp Khóa API để giao dịch từ xa.
* **Quyền Hạn Tối Thiểu (Least Privilege Principle)**: Khóa API chỉ được cấp quyền **Đọc (Read)** và **Giao Dịch (Trade)**. Tuyệt đối **không được bật** quyền **Rút Tiền (Withdraw)** dưới bất kỳ hình thức nào.

---

## 🛡️ 2. Chốt Chặn Quản Trị Rủi Ro Cứng (Risk Engine Safeguards)

Bộ Động cơ Quản lý rủi ro độc lập `RiskManager` (`domain/risk/risk_manager.py`) hoạt động như một bộ lọc bất biến:
* **Giới Hạn Đòn Bẩy (Leverage Cap)**: Đòn bẩy tối đa của lệnh gửi đi được giới hạn cứng bởi tham số `MAX_LEVERAGE` cấu hình trong `.env`. Nếu một thuật toán chiến lược yêu cầu đòn bẩy vượt ngưỡng này, `RiskManager` lập tức từ chối và phát đi sự kiện `risk.signal_rejected`.
* **Chế Độ Ký Quỹ Cô Lập (Isolated Margin Mode)**: Bot được lập trình để chỉ chạy chế độ ký quỹ cô lập. Nghĩa là, mỗi vị thế giao dịch chỉ chịu rủi ro trên số tiền ký quỹ được phân bổ riêng cho vị thế đó, hoàn toàn triệt tiêu nguy cơ cháy lan sang toàn bộ số dư tài khoản giao dịch chính.
* **Giới Hạn Tiền Ký Quỹ Mỗi Lệnh (Margin Cap Per Order)**: Tham số `MARGIN_PER_ORDER_USDT` giới hạn số vốn tối đa được phép phân bổ cho mỗi lệnh đặt mới. Giúp ngăn chặn các thảm họa từ lỗi thuật toán đặt lệnh quy mô quá lớn.

---

## 🔌 3. Xác Thực Vị Thế Ẩn (Shadow Validation)

Lớp Giám sát Ẩn `ShadowValidator` (`services/position/shadow_validator.py`) đóng vai trò như chốt chặn cuối cùng bảo vệ trước các lệnh giảm vị thế (Reduce-Only):
* Trước khi tiến hành gửi lệnh đóng vị thế (Chốt lời, Cắt lỗ hoặc đóng thủ công khẩn cấp), bot sẽ luôn thực hiện truy vấn đối chiếu số dư và kích thước vị thế thực tế đang mở.
* Nếu kích thước vị thế yêu cầu đóng lại lớn hơn kích thước đang thực sự tồn tại, `ShadowValidator` sẽ tự động cắt gọt hạ kích thước lệnh đóng xuống bằng đúng mức dư nợ đang có. Cơ chế này loại bỏ hoàn toàn các lỗi gửi lệnh với khối lượng quá đà, vốn là nguyên nhân tạo ra vị thế lật ngược chiều ngoài ý muốn (over-hedging).
* Đi kèm với tính năng này là công cụ **Gộp hợp đồng vụn (Dust Merge)** giúp quét sạch các hợp đồng không thể đóng lẻ do không đạt Kích thước lệnh tối thiểu của sàn.