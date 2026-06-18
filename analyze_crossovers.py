import re
from datetime import datetime

# Đọc toàn bộ log file
log_path = r'e:\PYTHON_OKX\vcorex_38PDL_12_06_ADX_THAN_NEN\logs\vcorex.log'
with open(log_path, 'r', encoding='utf-8') as f:
    log_content = f.read()

# Regex để trích xuất dữ liệu từ mỗi dòng [CROSSOVER-DETECTION]
pattern = r'\[CROSSOVER-DETECTION\] symbol=([A-Z]+-USDT-SWAP) timeframe=(\w+) fast_prev=([\d.]+) fast_completed=([\d.]+) slow_prev=([\d.]+) slow_completed=([\d.]+)'
matches = re.findall(pattern, log_content)

print(f"Tổng số dòng [CROSSOVER-DETECTION] tìm được: {len(matches)}")

# Phân tích từng crossover
real_crossovers = []
for match in matches:
    symbol, timeframe, fast_prev, fast_completed, slow_prev, slow_completed = match
    fast_prev = float(fast_prev)
    fast_completed = float(fast_completed)
    slow_prev = float(slow_prev)
    slow_completed = float(slow_completed)
    
    # Kiểm tra điều kiện crossover theo đúng yêu cầu
    bullish = fast_prev <= slow_prev and fast_completed > slow_completed
    bearish = fast_prev >= slow_prev and fast_completed < slow_completed
    
    if bullish or bearish:
        real_crossovers.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'bullish': bullish,
            'bearish': bearish,
            'fast_prev': fast_prev,
            'slow_prev': slow_prev,
            'fast_completed': fast_completed,
            'slow_completed': slow_completed,
            'timestamp': datetime.now()  # Trong log có timestamp thật, nhưng tạm dùng hiện tại
        })

# In ra tất cả crossover thực tế
print("\n=== DANH SÁCH CROSSOVER THỰC TẾ TRONG LOG ===")
for i, cross in enumerate(real_crossovers, 1):
    cross_type = "BULLISH" if cross['bullish'] else "BEARISH"
    print(f"{i}. {cross['symbol']} {cross['timeframe']} - {cross_type}")
    print(f"   EMA9(prev)={cross['fast_prev']:.4f}, EMA21(prev)={cross['slow_prev']:.4f}")
    print(f"   EMA9(current)={cross['fast_completed']:.4f}, EMA21(current)={cross['slow_completed']:.4f}")

# Thống kê theo symbol và timeframe
print("\n=== THỐNG KÊ CROSSOVER THEO SYMBOL VÀ TIMEFRAME ===")
stats = {}
for cross in real_crossovers:
    key = f"{cross['symbol']} | {cross['timeframe']}"
    if key not in stats:
        stats[key] = {'bullish': 0, 'bearish': 0, 'total': 0}
    if cross['bullish']:
        stats[key]['bullish'] +=1
    if cross['bearish']:
        stats[key]['bearish'] +=1
    stats[key]['total'] +=1

# In bảng thống kê
print(f"{'Symbol':<12} | {'TF':<6} | {'Bullish':<7} | {'Bearish':<7} | {'Total':<5}")
print("-" * 60)
for key, value in stats.items():
    symbol, tf = key.split(" | ")
    print(f"{symbol:<12} | {tf:<6} | {value['bullish']:<7} | {value['bearish']:<7} | {value['total']:<5}")

# Tổng kết
print(f"\nTổng số crossover thực tế phát hiện trong log: {len(real_crossovers)}")
total_bullish = sum(c['bullish'] for c in real_crossovers)
total_bearish = sum(c['bearish'] for c in real_crossovers)
print(f"Trong đó: Bullish={total_bullish}, Bearish={total_bearish}")

# Kiểm tra trường hợp EMA9 > EMA21 nhưng no_finalized_crossover
print("\n=== KIỂM TRA CÁC TRƯỜNG HỢP EMA9 > EMA21 NHƯNG KHÔNG CÓ CROSSOVER ===")
ema9_gt_ema21_no_cross = 0
for match in matches:
    symbol, timeframe, fast_prev, fast_completed, slow_prev, slow_completed = match
    fast_prev = float(fast_prev)
    fast_completed = float(fast_completed)
    slow_prev = float(slow_prev)
    slow_completed = float(slow_completed)
    
    # Kiểm tra EMA9 luôn lớn hơn EMA21 nhưng không có crossover
    if fast_prev > slow_prev and fast_completed > slow_completed:
        bullish = fast_prev <= slow_prev and fast_completed > slow_completed
        bearish = fast_prev >= slow_prev and fast_completed < slow_completed
        if not bullish and not bearish:
            ema9_gt_ema21_no_cross +=1

print(f"Số lần EMA9 > EMA21 nhưng không có crossover: {ema9_gt_ema21_no_cross}")
print(f"Lý do: Đây là trạng thái bình thường - EMA9 luôn nằm trên EMA21 (xu hướng tăng đang diễn ra), không có giao cắt mới xảy ra.")