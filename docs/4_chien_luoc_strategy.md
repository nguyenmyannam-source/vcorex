# 4. Chiến Lược & Rủi Ro | 4. Strategy & Risk

**Ngày cập nhật:** 13/06/2026
**Phiên bản:** 1.4
**Tác giả:** VCOREX Team
**Đồng bộ mã nguồn:** `services/strategies/ema_crossover.py`, `domain/risk/risk_manager.py`, `core/config/settings.py`, `services/market_data/indicators.py`

**Thay đổi v1.4:**
- Bổ sung tài liệu về cơ chế Gộp Hợp Đồng Rác (Dust Merge) trong phần Chốt lời / Cắt lỗ.
- Chuẩn hóa toàn bộ nội dung sang tiếng Việt chuyên ngành.

---

## 📖 Tóm Tắt Ngắn Gọn

Bot VCOREX sử dụng chiến lược **Giao cắt đường trung bình động hàm mũ (EMA Crossover)** để phát hiện tín hiệu giao dịch, kết hợp với nhiều bộ lọc để tránh tín hiệu giả (false positive) và quản trị rủi ro chặt chẽ.

**Quy trình hoạt động:**
1. **Phát hiện tín hiệu:** EMA nhanh cắt EMA chậm (Điểm giao cắt vàng - Golden Cross = Mua, Điểm giao cắt tử thần - Death Cross = Bán).
2. **Bộ lọc:** Kiểm tra kích thước thân nến, sức mạnh xu hướng qua chỉ báo ADX, và độ trễ của tín hiệu.
3. **Quản trị rủi ro:** Kiểm tra tỷ lệ đòn bẩy, số lượng vị thế tối đa, và mức độ sụt giảm tài khoản.
4. **Chốt lời/Cắt lỗ (TP/SL):** Tự động đặt các mốc Chốt lời và Cắt lỗ dựa trên Tỷ suất lợi nhuận trên vốn chủ sở hữu (ROE).

---

## 🧭 1. Chiến Lược Giao Cắt EMA (Cách bot phát hiện tín hiệu)

### Bot hoạt động như thế nào?

Bot liên tục theo dõi 2 đường EMA (Exponential Moving Average):
- **EMA nhanh (Chu kỳ 9 nến):** Nhạy bén với biến động giá hiện tại.
- **EMA chậm (Chu kỳ 21 nến):** Biểu diễn xu hướng ổn định trong dài hạn.

### Quy tắc giao dịch

| Tình huống giao cắt | Hành động thực thi | Ý nghĩa kỹ thuật |
|:---|:---|:---|
| **EMA nhanh cắt LÊN trên EMA chậm** | **MUA (Long)** | Bắt đầu xu hướng tăng ➔ Điểm giao cắt vàng (Golden Cross) |
| **EMA nhanh cắt XUỐNG dưới EMA chậm** | **BÁN (Short)** | Bắt đầu xu hướng giảm ➔ Điểm giao cắt tử thần (Death Cross) |

### Khung thời gian theo dõi (Timeframes)

Hệ thống theo dõi đồng thời 7 khung thời gian: `5m`, `15m`, `1H`, `4H`, `1D`, `1W`, `1M`.

---

## 🔍 2. Bộ Lọc Thân Nến (Tránh tín hiệu từ nến yếu)

### Tại sao cần lọc?

Nến nhỏ (thân nến ngắn) thường biểu hiện sự thiếu quyết đoán của thị trường và không có ý nghĩa giao dịch mạnh. Bot chỉ cấp phép vào lệnh khi thân nến đạt kích thước tiêu chuẩn.

### Công thức tính tỷ lệ thân nến

```
Tỷ lệ thân nến % = (Giá đóng cửa - Giá mở cửa) / (Giá cao nhất - Giá thấp nhất) × 100
```

### Ngưỡng tiêu chuẩn tối thiểu theo khung thời gian

| Khung thời gian | Thân nến tối thiểu |
| :--- | :--- |
| 5 phút | 1.0% |
| 15 phút | 2.5% |
| 1 giờ | 5.0% |
| 4 giờ | 6.0% |
| 1 ngày | 7.5% |
| 1 tuần | 10.0% |
| 1 tháng | 12.0% |

**Quy tắc:** Nếu thân nến nhỏ hơn ngưỡng quy định, bot sẽ từ chối tín hiệu với lý do `body_too_small`.

---

## ⚡ 3. Chế Độ Xác Nhận Tín Hiệu (Confirmation Candles)

### Biến cấu hình CONFIRMATION_CANDLES là gì?

Biến số `CONFIRMATION_CANDLES` quyết định **thời điểm bot vào lệnh** ngay sau khi phát hiện sự giao cắt EMA.

**⚠️ QUAN TRỌNG: Kể từ phiên bản v1.3, hệ thống CHỈ CÒN hỗ trợ Chế độ Xác nhận (CONFIRMATION MODE = 1) cho TẤT CẢ các khung thời gian.**

- **Chế độ thời gian thực (Realtime Mode = 0):** ❌ KHÔNG HỖ TRỢ NỮA. (Đã bị loại bỏ hoàn toàn để tránh rủi ro do nến đang hình thành).
- **Chế độ Xác nhận (Confirmation Mode = 1):** ✅ MẶC ĐỊNH CHO MỌI KHUNG GIỜ. Chỉ tính toán và xử lý tín hiệu dựa trên nến đã đóng cửa hoàn toàn (chỉ số mảng `candles[-2]`).

### Lý do loại bỏ Chế độ Thời gian thực:
- Ngăn chặn triệt để lỗi **Thiên kiến nhìn trước (Look-ahead bias)**.
- Tránh việc các chỉ báo bị tính toán lệch pha trên các cây nến khác nhau.
- Giảm thiểu tối đa tín hiệu giả (false positive).
- Đảm bảo tính nhất quán tuyệt đối của ảnh chụp dữ liệu (`snapshot.validate_consistency()`).

---

## 📈 4. Bộ Lọc ADX (Kiểm tra sức mạnh xu hướng)

### Chỉ báo ADX là gì?

ADX (Average Directional Index) dùng để đo **sức mạnh** của một xu hướng hiện tại, độc lập với việc xu hướng đó là tăng hay giảm.

- **ADX cao:** Xu hướng mạnh (Môi trường lý tưởng để giao dịch).
- **ADX thấp:** Xu hướng yếu hoặc đi ngang (Cần tránh giao dịch).

### Quy tắc lọc ADX

| Điều kiện ADX | Quyết định của hệ thống |
|:---|:---|
| ADX = 0 (Chưa đủ lượng nến để tính toán) | Tạm bỏ qua bộ lọc này |
| ADX > 0 nhưng < ngưỡng quy định | **Từ chối** tín hiệu (Xu hướng quá yếu) |
| ADX ≥ ngưỡng quy định | **Chấp nhận** tín hiệu (Xu hướng đủ mạnh) |

### Ngưỡng ADX theo khung thời gian

| Khung thời gian | Ngưỡng ADX |
|:---|:---|
| 5m, 15m, 1H | 25.0 |
| 4H, 1D, 1W, 1M | 20.0 |

---

## 💰 5. Chốt Lời & Cắt Lỗ (Take Profit & Stop Loss)

### Quy mô lệnh giao dịch (Position Sizing)

```
Quy mô (Volume) = Mức ký quỹ mục tiêu (MARGIN_PER_ORDER_USDT) × Đòn bẩy (DEFAULT_LEVERAGE)
Ví dụ: $1,000 × 10x = $10,000
```

### Cắt Lỗ (Stop Loss)

- **Mặc định:** Cắt lỗ toàn bộ khi tỷ suất lợi nhuận (ROE) âm **50%**.
- **Công thức tính giá Cắt lỗ dự kiến:**
  ```
  Biến động giá % = (Tỷ lệ ROE Cắt Lỗ + Biên độ Phí giao dịch) / (100 × Đòn bẩy)
  ```

### Chốt Lời Đa Điểm (Take Profit) - Phân ra 3 mốc

Hệ thống sẽ chia nhỏ vị thế thành 3 phần để chốt lời dần nhằm tối ưu hóa lợi nhuận:

| Mốc | Lợi nhuận ROE mục tiêu | Tỷ lệ đóng vị thế |
|:---|:---|:---|
| TP1 | 50% | Đóng 50% khối lượng lệnh |
| TP2 | 100% | Đóng 30% khối lượng lệnh |
| TP3 | 150% | Đóng 20% khối lượng lệnh |

### Cơ chế tự động Gộp Hợp Đồng Rác (Dust Merge)

Sàn giao dịch (OKX) có quy định rất nghiêm ngặt về **Kích thước lệnh tối thiểu (Minimum lot size / sz)**. Nếu vị thế còn lại sau khi chốt lời TP2 quá nhỏ (dưới mức tối thiểu của sàn), lệnh TP3 sẽ không thể thực thi được và để lại hợp đồng rác (dust contracts). 

**Giải pháp:** Hệ thống đã tích hợp **Trình giải quyết Chốt lời (TP/SL Resolver)**. Trình này tự động kiểm tra khối lượng của lệnh TP cuối cùng. Nếu phát hiện khối lượng nhỏ hơn kích thước lô tối thiểu, hệ thống sẽ tự động cộng dồn phần rác đó vào mốc chốt lời trước đó (TP2). Điều này đảm bảo khi chạm mốc giá trị, vị thế sẽ được đóng hoàn toàn sạch sẽ.

---

## 🛡️ 6. Quản Trị Rủi Ro (Bảo Vệ Tài Khoản)

### Kiểm tra bắt buộc (Luôn kích hoạt ở cả chế độ Demo và Thực tế)

| Tiêu chí | Mục đích bảo vệ |
|:---|:---|
| **Máy Trạm Đối Chiếu (Mirror)** | Đảm bảo đồng bộ xong dữ liệu trước khi hành động. |
| **Nồng độ Vốn Tập Trung (Symbol Concentration)** | Không vượt quá vị thế cho phép trên một loại tài sản. |
| **Mức Sụt Giảm Tối Đa (Max daily drawdown)** | Khóa giao dịch nếu sụt giảm quá 30% trong một ngày. |

### Các tiêu chuẩn khắt khe bổ sung (Chỉ kích hoạt ở chế độ Thực Tế - Production)

Khi biến số `PRODUCTION_RISK_MODE=true` được bật:

| Tiêu chí | Mục đích | Cấu hình mặc định |
|:---|:---|:---|
| Mức Ký Quỹ khả dụng | Phải đủ tiền nhàn rỗi để mở lệnh | — |
| Đòn Bẩy Tối Đa | Ngăn chặn sử dụng đòn bẩy nguy hiểm | 10x |
| Tỷ lệ Rủi ro / Vốn tổng | Giới hạn số tiền sẵn sàng mất trên một lệnh | 0.20% vốn chủ sở hữu |
| Tỷ lệ Lợi nhuận / Rủi ro (Risk:Reward) | Khoản lãi mục tiêu phải lớn hơn mức thua lỗ dự kiến | ≥ 1.5 |

---

## 📡 7. Cảnh Báo Biến Động Giá (Volatility Alert)

Hệ thống theo dõi biên độ dao động và lập tức phát đi cảnh báo tới Telegram nếu giá thị trường nhảy vọt quá nhanh trong một khung thời gian ngắn.

| Phân cấp đồng tiền (Tier) | Biến động nến 5m | Biến động 1 phút |
|:---|:---|:---|
| **Tier 1 - Vốn hóa siêu khổng lồ** (BTC, ETH) | 2.0% | 1.5% |
| **Tier 2 - Vốn hóa lớn** (SOL, BNB, XRP...) | 4.0% | 3.0% |
| **Tier 3 - Các đồng Altcoin khác** | 6.0% | 4.0% |

---

## ⏰ 8. Phát Hiện Tín Hiệu Trễ (Stale Signal)

Nếu bot gặp tình trạng quá tải hoặc nghẽn mạng và phân tích tín hiệu chậm hơn tiêu chuẩn định mức sau khi nến đã đóng, giá trị thực tế đã có thể trôi quá xa. Tín hiệu này sẽ bị loại bỏ với lý do `stale_signal`.

| Khung thời gian nến | Thời gian trễ tối đa cho phép |
|:---|:---|
| 5 phút | 60 giây |
| 15 phút | 180 giây (3 phút) |
| 1 giờ | 720 giây (12 phút) |

---

## 📋 Phụ lục - Các lý do từ chối tín hiệu thường gặp

| Mã lỗi từ chối | Giải nghĩa |
|:---|:---|
| `no_finalized_crossover` | Chưa hình thành điểm giao cắt hoàn chỉnh trên nến đóng. |
| `indicator_bundle_mismatch` | Lệch pha thời gian giữa các mảng dữ liệu chỉ báo (Các thành phần EMA/ADX/Thân nến không đồng nhất). |
| `body_too_small` | Kích thước thân nến quá yếu, không thể hiện đủ lực. |
| `weak_trend_adx` | Chỉ số sức mạnh xu hướng ADX quá thấp. |
| `color_validation_failed` | Màu nến (tăng/giảm) đi ngược lại với hướng cắt của EMA. |
| `stale_signal` | Tín hiệu đã quá hạn xử lý (máy chủ bị trễ nhịp). |
| `snapshot_inconsistency` | Dữ liệu chụp nhanh bị đứt gãy, hệ thống từ chối vào lệnh để bảo đảm an toàn tuyệt đối. |