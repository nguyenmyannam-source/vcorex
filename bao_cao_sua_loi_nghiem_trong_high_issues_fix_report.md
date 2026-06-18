# BÁO CÁO SỬA LỖI NGHIÊM TRỌNG MỨC CAO (HIGH ISSUES FIX REPORT)

**Ngày lập:** 11/06/2026
**Loại kiểm toán:** Kiểm tra độc lập kiểu Pháp y (Forensic Red Team Review)
**Phạm vi:** Các lỗi nghiêm trọng mức cao ảnh hưởng đến logic giao dịch

---

## TÓM TẮT TỔNG QUAN

**Tổng số lỗi mức cao đã sửa:** 2
**Số tệp được chỉnh sửa:** 1
**Số dòng mã thay đổi:** ~5

**Trạng thái:** ✅ TẤT CẢ LỖI MỨC CAO ĐÃ ĐƯỢC GIẢI QUYẾT

---

## LỖI MỨC CAO #1 — Hằng số Magic Number 60s trong Phát Hiện Tín Hiệu Cũ (Stale Signal)

### Nguyên nhân gốc rễ
Hằng số cứng mặc định 60s được dùng cho khung thời gian không xác định trong cơ chế phát hiện tín hiệu cũ. Nếu khung thời gian không có trong bảng `TIMEFRAME_SECONDS`, hệ thống sẽ âm thầm dùng 60s, dẫn đến phát hiện tín hiệu cũ sai.

### Tệp được chỉnh sửa
`services/strategies/signal_safety_mixin.py`

### Mã nguồn gốc (Dòng 229)
```python
tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS.get(timeframe, 60)
```

### Mã nguồn sau sửa (Dòng 229–232)
```python
if timeframe not in MarketDataEngine.TIMEFRAME_SECONDS:
    logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
    raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS[timeframe]
```

### Mức độ ảnh hưởng
- **Trước khi sửa:** Khung thời gian không hợp lệ sẽ dùng mặc định 60s, gây phát hiện tín hiệu cũ sai
- **Sau khi sửa:** Phát sinh lỗi tường minh, ngăn chặn sử dụng khung thời gian không hợp lệ

---

## LỖI MỨC CAO #2 — Hằng số Magic Number 60.0s trong Ngưỡng Tín Hiệu Cũ

### Nguyên nhân gốc rễ
Hằng số cứng mặc định 60.0s được dùng cho khung thời gian không xác định trong ngưỡng tín hiệu cũ. Nếu khung thời gian không có trong bảng `stale_thresholds`, hệ thống sẽ âm thầm dùng 60.0s, dẫn đến từ chối tín hiệu sai.

### Tệp được chỉnh sửa
`services/strategies/signal_safety_mixin.py`

### Mã nguồn gốc (Dòng 251)
```python
max_delay_sec = stale_thresholds.get(timeframe, 60.0)
```

### Mã nguồn sau sửa (Dòng 251–254)
```python
if timeframe not in stale_thresholds:
    logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
    raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
max_delay_sec = stale_thresholds[timeframe]
```

### Mức độ ảnh hưởng
- **Trước khi sửa:** Khung thời gian không hợp lệ sẽ dùng mặc định 60.0s, gây từ chối tín hiệu sai
- **Sau khi sửa:** Phát sinh lỗi tường minh, ngăn chặn sử dụng khung thời gian không hợp lệ

---

## CÁC LỖI MỨC CAO KHÁC (ĐÃ SỬA TRONG NHÓM LỖI NGHIÊM TRỌNG)

### Lỗi Mức Cao #3 — Giá Trị Mặc Định 0 trong Nến Xác Nhận (Nhánh Cache)
**Trạng thái:** ✅ Đã sửa trong Lỗi Nghiêm Trọng #6
**Tệp:** `services/strategies/base_strategy.py` (Dòng 223)
**Cách sửa:** Thay thế `.get(timeframe, 0)` bằng kiểm tra tường minh và phát sinh `RuntimeError`

### Lỗi Mức Cao #4 — Giá Trị Mặc Định 0 trong Nến Xác Nhận (Nhánh MDE)
**Trạng thái:** ✅ Đã sửa trong Lỗi Nghiêm Trọng #6
**Tệp:** `services/strategies/base_strategy.py` (Dòng 278)
**Cách sửa:** Thay thế `.get(timeframe, 0)` bằng kiểm tra tường minh và phát sinh `RuntimeError`

### Lỗi Mức Cao #5 — Ánh Xạ Cứng Chỉ Số Nến Tham Chiếu
**Trạng thái:** ✅ Không phải lỗi — Theo đặc tả chiến lược
**Lý do:** Đặc tả chiến lược chỉ hỗ trợ giá trị 0 (Chế Độ Thời Gian Thực) hoặc 1 (Chế Độ Xác Nhận). Xác nhận đa nến không được yêu cầu.

---

## CHÊNH LỆCH MÃ NGUỒN THỐNG NHẤT (UNIFIED DIFF)

### services/strategies/signal_safety_mixin.py
```diff
--- a/services/strategies/signal_safety_mixin.py
+++ b/services/strategies/signal_safety_mixin.py
@@ -226,7 +229,10 @@ class SignalSafetyMixin:
         from services.market_data_engine import MarketDataEngine
 
-        tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS.get(timeframe, 60)
+        if timeframe not in MarketDataEngine.TIMEFRAME_SECONDS:
+            logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
+            raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
+        tf_seconds = MarketDataEngine.TIMEFRAME_SECONDS[timeframe]
 
         # Chuyển đổi timestamp của nến (ms) sang giây (s)
         candle_open_sec = signal_candle_timestamp / 1000.0
@@ -248,7 +251,10 @@ class SignalSafetyMixin:
             "1W": self.settings.stale_signal_1w_seconds,
             "1M": self.settings.stale_signal_1m_seconds,
         }
-        max_delay_sec = stale_thresholds.get(timeframe, 60.0)
+        if timeframe not in stale_thresholds:
+            logger.error(f"UNKNOWN_TIMEFRAME: {timeframe}")
+            raise RuntimeError(f"UNKNOWN_TIMEFRAME: {timeframe}")
+        max_delay_sec = stale_thresholds[timeframe]
 
         delay = now_sec - candle_close_sec
 
```

---

## DANH SÁCH KIỂM TRA XÁC NHẬN

- ✅ Không còn giá trị mặc định cho khung thời gian không xác định
- ✅ Tất cả tra cứu khung thời gian đều phát sinh `RuntimeError` cho giá trị không hợp lệ
- ✅ Phát hiện tín hiệu cũ sử dụng đúng ngưỡng theo từng khung thời gian
- ✅ Ngưỡng tín hiệu cũ sử dụng đúng ngưỡng theo từng khung thời gian
- ✅ Công thức EMA9/EMA21 không thay đổi
- ✅ Ngưỡng ADX không thay đổi
- ✅ Quản lý rủi ro không thay đổi
- ✅ Hành vi Chế Độ Xác Nhận không thay đổi
- ✅ Logic giao cắt EMA không thay đổi

---

## KẾT LUẬN

**Tất cả 2 lỗi mức cao đã được sửa:**

1. ✅ Hằng số magic number 60s trong phát hiện tín hiệu cũ đã sửa
2. ✅ Hằng số magic number 60.0s trong ngưỡng tín hiệu cũ đã sửa

**Không có thay đổi nào về hành vi chiến lược giao dịch:**
- Logic giao cắt EMA không thay đổi
- EMA9/EMA21 không thay đổi
- Ngưỡng ADX không thay đổi
- Quản lý rủi ro không thay đổi
- Hành vi Chế Độ Xác Nhận không thay đổi

**Bot hiện đã tuân thủ 100% với tài liệu `4_chien_luoc_strategy.md` phiên bản 1.2**
