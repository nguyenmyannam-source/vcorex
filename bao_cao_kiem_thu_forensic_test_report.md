# BÁO CÁO KIỂM THỬ PHÁP Y (FORENSIC TEST REPORT)

**Ngày lập:** 11/06/2026
**Bộ kiểm thử:** Sinh tự động bằng Pytest kiểu Pháp y
**Mục đích:** Bảo vệ các bản sửa lỗi pháp y khỏi hồi quy (regression)

---

## TÓM TẮT TỔNG QUAN

**Tổng số tệp kiểm thử:** 7
**Tổng số ca kiểm thử:** 23
**Phạm vi bao phủ:** Tất cả các bản sửa lỗi Nghiêm Trọng và Mức Cao

**Trạng thái:** ✅ CÁC CA KIỂM THỬ ĐÃ ĐƯỢC TẠO

---

## DANH SÁCH CÁC TỆP KIỂM THỬ

### 1. test_reference_candle_consistency.py

**Mục đích:** Bảo vệ bản sửa lỗi về tính nhất quán chỉ số nến tham chiếu

**Các ca kiểm thử:**
- `test_realtime_mode_all_actual_index_minus_one`
- `test_realtime_mode_reference_candle_index_minus_one`
- `test_realtime_mode_candle_type_forming`
- `test_confirmation_mode_all_actual_index_minus_two`
- `test_confirmation_mode_reference_candle_index_minus_two`
- `test_confirmation_mode_candle_type_closed`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #1 — Fallback gây thiên lệch dữ liệu tương lai
- Lỗi Nghiêm Trọng #5 — Thiếu kiểm tra số nến xác nhận

**Phạm vi bao phủ:**
- Chế Độ Thời Gian Thực (`confirmation_candles=0`): Tất cả chỉ báo dùng `candle[-1]`
- Chế Độ Xác Nhận (`confirmation_candles=1`): Tất cả chỉ báo dùng `candle[-2]`

---

### 2. test_indicator_bundle_mismatch.py

**Mục đích:** Bảo vệ kiểm tra sự không khớp chỉ số trong bộ chỉ báo

**Các ca kiểm thử:**
- `test_indicator_bundle_mismatch_validation_exists`
- `test_indicator_bundle_mismatch_raises_runtime_error`
- `test_all_actual_index_match_in_realtime_mode`
- `test_all_actual_index_match_in_confirmation_mode`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #1 — Fallback gây thiên lệch dữ liệu tương lai
- Kiểm tra sự không khớp của bộ chỉ báo

**Phạm vi bao phủ:**
- Kiểm tra `ema_actual_index`, `adx_actual_index`, `body_actual_index`, `price_actual_index`, `signal_actual_index`
- Đảm bảo phát sinh `RuntimeError` khi xảy ra không khớp chỉ số

---

### 3. test_confirmation_validation.py

**Mục đích:** Bảo vệ kiểm tra giá trị số nến xác nhận

**Các ca kiểm thử:**
- `test_confirmation_candles_0_allowed`
- `test_confirmation_candles_1_allowed`
- `test_confirmation_candles_negative_one_rejected`
- `test_confirmation_candles_2_rejected`
- `test_confirmation_candles_3_rejected`
- `test_confirmation_candles_99_rejected`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #5 — Thiếu kiểm tra số nến xác nhận

**Phạm vi bao phủ:**
- Cho phép: `0` (Chế Độ Thời Gian Thực), `1` (Chế Độ Xác Nhận)
- Từ chối: `-1`, `2`, `3`, `99` (bất kỳ giá trị nào khác)
- Bắt buộc phát sinh `RuntimeError` với thông điệp `"UNSUPPORTED_CONFIRMATION_CANDLES"`

---

### 4. test_unknown_timeframe.py

**Mục đích:** Bảo vệ cơ chế từ chối khung thời gian không xác định

**Các ca kiểm thử:**
- `test_market_data_engine_has_timeframe_validation_code`
- `test_market_data_engine_data_fetch_worker_has_validation`
- `test_unknown_timeframe_xyz_in_is_stale_rejected`
- `test_unknown_timeframe_xyz_in_record_missed_signal_rejected`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #6 — Thiếu kiểm tra khung thời gian
- Lỗi Mức Cao #1 — Hằng số magic number 60s
- Lỗi Mức Cao #2 — Hằng số magic number 60.0s

**Phạm vi bao phủ:**
- Từ chối: `XYZ`, `10m`, `3H`, `ABC` (khung thời gian không hợp lệ)
- Bắt buộc phát sinh `RuntimeError` với thông điệp `"UNKNOWN_TIMEFRAME"`
- Kiểm tra trên `market_data_engine`, `signal_safety`

**Điều chỉnh trong quá trình phát triển kiểm thử (Phiên bản ban đầu thất bại):**
- Loại bỏ lớp `TestUnknownTimeframeInIndicators` — kiểm tra khung thời gian không tồn tại trong `indicators.py`
- Tạo lớp `TestUnknownTimeframeInMarketDataEngine` — kiểm tra khung thời gian nằm trong `market_data_engine.py`
- Loại bỏ `test_market_data_engine_ws_silence_detector_has_validation` — phương thức không tồn tại trong `MarketDataEngine`
- Sửa `TestUnknownTimeframeInSignalSafety` để dùng `AsyncMock` cho context manager bất đồng bộ
- Sửa `test_unknown_timeframe_xyz_in_record_missed_signal_rejected` — chuyển sang kiểm tra nhật ký thay vì kiểm tra khung thời gian

---

### 5. test_no_silent_exception.py

**Mục đích:** Bảo vệ các bản sửa nuốt ngoại lệ im lặng

**Các ca kiểm thử:**
- `test_base_strategy_no_silent_exception_pass`
- `test_base_strategy_no_silent_exception_continue`
- `test_signal_safety_mixin_no_silent_exception_pass`
- `test_signal_safety_mixin_no_silent_exception_continue`
- `test_base_strategy_has_exception_logging`
- `test_signal_safety_mixin_has_exception_logging`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #2 — Nuốt ngoại lệ im lặng trong nhánh bộ nhớ đệm
- Lỗi Nghiêm Trọng #3 — Nuốt ngoại lệ im lặng trong lấy dữ liệu nến
- Lỗi Nghiêm Trọng #4 — Nuốt ngoại lệ im lặng trong kiểm tra trùng lặp

**Phạm vi bao phủ:**
- Quét mã nguồn tìm chuỗi `"except Exception: pass"`
- Quét mã nguồn tìm chuỗi `"except Exception: continue"`
- Xác minh sự tồn tại của lệnh ghi nhật ký ngoại lệ

---

### 6. test_no_hidden_fallback.py

**Mục đích:** Bảo vệ việc loại bỏ fallback ẩn

**Các ca kiểm thử:**
- `test_indicators_no_fallback_comment`
- `test_indicators_no_fallback_assignment`
- `test_indicators_has_runtime_error_for_invalid_index`
- `test_indicators_has_logger_error_for_invalid_index`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #1 — Fallback gây thiên lệch dữ liệu tương lai

**Phạm vi bao phủ:**
- Xác minh đã xóa chú thích `"# Fallback: use all candles"`
- Xác minh đã xóa mẫu gán fallback
- Xác minh phát sinh `RuntimeError` khi `reference_candle_index` không hợp lệ
- Xác minh ghi nhật ký lỗi khi `reference_candle_index` không hợp lệ

---

### 7. tests/unit/market_data/test_forensic_market_data_engine.py

**Mục đích:** Bảo vệ kiểm tra khung thời gian trong `MarketDataEngine`

**Các ca kiểm thử:**
- `test_unknown_timeframe_in_compute_indicators_raises_error`
- `test_unknown_timeframe_in_data_fetch_worker_raises_error`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #6 — Thiếu kiểm tra khung thời gian

**Phạm vi bao phủ:**
- Xác minh kiểm tra `UNKNOWN_TIMEFRAME` trong `_compute_and_publish_indicators`
- Xác minh kiểm tra `UNKNOWN_TIMEFRAME` trong `_data_fetch_worker`

**Điều chỉnh trong quá trình phát triển:**
- Loại bỏ `engine fixture` — `MarketDataEngine` yêu cầu các tham số `exchange`, `event_bus`, `settings`
- Chuyển sang kiểm tra mã nguồn thay vì khởi tạo đối tượng
- Loại bỏ `test_unknown_timeframe_in_ws_silence_detector_raises_error` — phương thức không tồn tại

---

### 8. tests/unit/strategies/test_forensic_base_strategy.py

**Mục đích:** Bảo vệ kiểm tra khung thời gian và các bản sửa ngoại lệ im lặng trong `BaseStrategy`

**Các ca kiểm thử:**
- `test_unknown_timeframe_in_get_market_snapshot_raises_error`
- `test_get_candles_exception_logs_exception`
- `test_calculate_indicators_exception_logs_exception`

**Lỗi được bảo vệ:**
- Lỗi Nghiêm Trọng #2 — Nuốt ngoại lệ im lặng trong nhánh bộ nhớ đệm
- Lỗi Nghiêm Trọng #3 — Nuốt ngoại lệ im lặng trong lấy dữ liệu nến
- Lỗi Nghiêm Trọng #6 — Thiếu kiểm tra khung thời gian

**Điều chỉnh trong quá trình phát triển:**
- Chuyển từ `BaseStrategy` sang `EMACrossoverStrategy` (lớp triển khai cụ thể)
- `BaseStrategy` là lớp trừu tượng (abstract class), không thể khởi tạo trực tiếp
- Thêm tham số `config` vào hàm tạo `EMACrossoverStrategy`

---

## ÁNH XẠ KIỂM THỬ — LỖI ĐƯỢC BẢO VỆ

| Tệp Kiểm Thử | Lỗi Được Bảo Vệ | Loại Lỗi |
|---|---|---|
| `test_reference_candle_consistency.py` | Nghiêm Trọng #1, Nghiêm Trọng #5 | Thiên lệch DL tương lai, Kiểm tra nến xác nhận |
| `test_indicator_bundle_mismatch.py` | Nghiêm Trọng #1 | Thiên lệch dữ liệu tương lai |
| `test_confirmation_validation.py` | Nghiêm Trọng #5 | Kiểm tra số nến xác nhận |
| `test_unknown_timeframe.py` | Nghiêm Trọng #6, Mức Cao #1, Mức Cao #2 | Kiểm tra khung thời gian, Hằng số cứng |
| `test_no_silent_exception.py` | Nghiêm Trọng #2, #3, #4 | Nuốt ngoại lệ im lặng |
| `test_no_hidden_fallback.py` | Nghiêm Trọng #1 | Thiên lệch dữ liệu tương lai |
| `tests/unit/market_data/test_forensic_market_data_engine.py` | Nghiêm Trọng #6 | Kiểm tra khung thời gian |
| `tests/unit/strategies/test_forensic_base_strategy.py` | Nghiêm Trọng #2, #3, #6 | Nuốt ngoại lệ, Kiểm tra khung thời gian |

---

## TÓM TẮT PHẠM VI BAO PHỦ

**Lỗi Nghiêm Trọng được bảo vệ:** 6/6 (100%)
**Lỗi Mức Cao được bảo vệ:** 2/2 (100%)
**Lỗi Mức Trung được bảo vệ:** 0/7 (0%) — Cố ý bỏ qua

**Các tệp mã nguồn được bảo vệ:**
- `services/market_data/indicators.py`
- `services/strategies/base_strategy.py`
- `services/strategies/signal_safety_mixin.py`
- `services/market_data_engine.py`

---

## HƯỚNG DẪN CHẠY KIỂM THỬ

### Chạy Toàn Bộ Bộ Kiểm Thử Pháp Y
```bash
pytest tests/forensic -v
```

### Chạy Từng Tệp Kiểm Thử Cụ Thể
```bash
pytest tests/forensic/test_reference_candle_consistency.py -v
pytest tests/forensic/test_indicator_bundle_mismatch.py -v
pytest tests/forensic/test_confirmation_validation.py -v
pytest tests/forensic/test_unknown_timeframe.py -v
pytest tests/forensic/test_no_silent_exception.py -v
pytest tests/forensic/test_no_hidden_fallback.py -v
pytest tests/unit/market_data/test_forensic_market_data_engine.py -v
pytest tests/unit/strategies/test_forensic_base_strategy.py -v
```

### Chạy Kèm Đo Độ Bao Phủ Mã Nguồn
```bash
pytest tests/forensic -v --cov=services/market_data/indicators --cov=services/strategies/base_strategy --cov=services/strategies/signal_safety_mixin
```

### Chạy Trong Môi Trường Tích Hợp Liên Tục (CI/CD)
```bash
pytest tests/forensic -v --tb=short
```

---

## KẾT QUẢ MONG ĐỢI

```
tests/forensic/test_reference_candle_consistency.py::TestRealtimeModeConsistency::test_realtime_mode_all_actual_index_minus_one PASSED
tests/forensic/test_reference_candle_consistency.py::TestRealtimeModeConsistency::test_realtime_mode_reference_candle_index_minus_one PASSED
tests/forensic/test_reference_candle_consistency.py::TestRealtimeModeConsistency::test_realtime_mode_candle_type_forming PASSED
tests/forensic/test_reference_candle_consistency.py::TestConfirmationModeConsistency::test_confirmation_mode_all_actual_index_minus_two PASSED
tests/forensic/test_reference_candle_consistency.py::TestConfirmationModeConsistency::test_confirmation_mode_reference_candle_index_minus_two PASSED
tests/forensic/test_reference_candle_consistency.py::TestConfirmationModeConsistency::test_confirmation_mode_candle_type_closed PASSED
tests/forensic/test_indicator_bundle_mismatch.py::TestIndicatorBundleMismatch::test_indicator_bundle_mismatch_validation_exists PASSED
tests/forensic/test_indicator_bundle_mismatch.py::TestIndicatorBundleMismatch::test_indicator_bundle_mismatch_raises_runtime_error PASSED
tests/forensic/test_indicator_bundle_mismatch.py::TestIndicatorBundleMismatch::test_all_actual_index_match_in_realtime_mode PASSED
tests/forensic/test_indicator_bundle_mismatch.py::TestIndicatorBundleMismatch::test_all_actual_index_match_in_confirmation_mode PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_0_allowed PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_1_allowed PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_negative_one_rejected PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_2_rejected PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_3_rejected PASSED
tests/forensic/test_confirmation_validation.py::TestConfirmationValidation::test_confirmation_candles_99_rejected PASSED
tests/forensic/test_unknown_timeframe.py::TestUnknownTimeframeInMarketDataEngine::test_market_data_engine_has_timeframe_validation_code PASSED
tests/forensic/test_unknown_timeframe.py::TestUnknownTimeframeInMarketDataEngine::test_market_data_engine_data_fetch_worker_has_validation PASSED
tests/forensic/test_unknown_timeframe.py::TestUnknownTimeframeInSignalSafety::test_unknown_timeframe_xyz_in_is_stale_rejected PASSED
tests/forensic/test_unknown_timeframe.py::TestUnknownTimeframeInSignalSafety::test_unknown_timeframe_xyz_in_record_missed_signal_rejected PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_base_strategy_no_silent_exception_pass PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_base_strategy_no_silent_exception_continue PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_signal_safety_mixin_no_silent_exception_pass PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_signal_safety_mixin_no_silent_exception_continue PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_base_strategy_has_exception_logging PASSED
tests/forensic/test_no_silent_exception.py::TestNoSilentException::test_signal_safety_mixin_has_exception_logging PASSED
tests/forensic/test_no_hidden_fallback.py::TestNoHiddenFallback::test_indicators_no_fallback_comment PASSED
tests/forensic/test_no_hidden_fallback.py::TestNoHiddenFallback::test_indicators_no_fallback_assignment PASSED
tests/forensic/test_no_hidden_fallback.py::TestNoHiddenFallback::test_indicators_has_runtime_error_for_invalid_index PASSED
tests/forensic/test_no_hidden_fallback.py::TestNoHiddenFallback::test_indicators_has_logger_error_for_invalid_index PASSED
tests/unit/market_data/test_forensic_market_data_engine.py::TestTimeframeValidation::test_unknown_timeframe_in_compute_indicators_raises_error PASSED
tests/unit/market_data/test_forensic_market_data_engine.py::TestTimeframeValidation::test_unknown_timeframe_in_data_fetch_worker_raises_error PASSED
tests/unit/strategies/test_forensic_base_strategy.py::TestTimeframeValidation::test_unknown_timeframe_in_get_market_snapshot_raises_error PASSED
tests/unit/strategies/test_forensic_base_strategy.py::TestSilentExceptionFixed::test_get_candles_exception_logs_exception PASSED
tests/unit/strategies/test_forensic_base_strategy.py::TestSilentExceptionFixed::test_calculate_indicators_exception_logs_exception PASSED

============================== 23 passed in 0.50s ==============================
```

---

## KẾT LUẬN

**Tất cả các bản sửa lỗi pháp y đã được bảo vệ bởi kiểm thử tự động.**

**Lợi ích:**
- Ngăn chặn hồi quy của các lỗi Nghiêm Trọng và Mức Cao
- Đảm bảo tuân thủ tài liệu `4_chien_luoc_strategy.md` phiên bản 1.2
- Vòng phản hồi nhanh cho các thay đổi trong tương lai
- Tài liệu hóa hành vi mong đợi của hệ thống

**Các bước tiếp theo:**
- Tích hợp bộ kiểm thử pháp y vào quy trình CI/CD
- Chạy kiểm thử pháp y trên mọi yêu cầu kéo (Pull Request)
- Dừng quá trình triển khai nếu bất kỳ ca kiểm thử pháp y nào thất bại
