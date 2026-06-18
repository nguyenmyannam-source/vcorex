import re
from datetime import datetime

log_path = r'e:\PYTHON_OKX\vcorex_38PDL_12_06_ADX_THAN_NEN\logs\vcorex.log'
with open(log_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Thu thập tất cả các block log chứa [CROSSOVER-DETECTION]
crossover_blocks = []
current_block = []
for line in lines:
    if '[CROSSOVER-DETECTION]' in line:
        if current_block:
            crossover_blocks.append(' '.join(current_block))
        current_block = [line.strip()]
    elif current_block:
        current_block.append(line.strip())
if current_block:
    crossover_blocks.append(' '.join(current_block))

print(f"Tổng số block CROSSOVER-DETECTION: {len(crossover_blocks)}")

# Phân tích từng block
real_crossovers = []
all_evaluations = 0

for block in crossover_blocks:
    # Trích xuất các giá trị từ block đã nối lại
    symbol_match = re.search(r'symbol=([A-Z]+-USDT-SWAP)', block)
    tf_match = re.search(r'timeframe=(\w+)', block)
    fast_prev_match = re.search(r'fast_prev=([\d.]+)', block)
    fast_completed_match = re.search(r'fast_completed=([\d.]+)', block)
    slow_prev_match = re.search(r'slow_prev=([\d.]+)', block)
    slow_completed_match = re.search(r'slow_completed=([\d.]+)', block)
    
    if not all([symbol_match, tf_match, fast_prev_match, fast_completed_match, slow_prev_match, slow_completed_match]):
        continue
        
    symbol = symbol_match.group(1)
    timeframe = tf_match.group(1)
    fast_prev = float(fast_prev_match.group(1))
    fast_completed = float(fast_completed_match.group(1))
    slow_prev = float(slow_prev_match.group(1))
    slow_completed = float(slow_completed_match.group(1))
    
    all_evaluations +=1
    
    # Kiểm tra điều kiện crossover
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
            'slow_completed': slow_completed
        })

# In kết quả
print("\n=== KẾT QUẢ PHÂN TÍCH CROSSOVER ===")
print(f"Tổng số lần đánh giá tín hiệu: {all_evaluations}")
print(f"Tổng số crossover thực tế (TOTAL_REAL_CROSSOVERS): {len(real_crossovers)}")

if real_crossovers:
    print("\nChi tiết các crossover phát hiện:")
    for i, cross in enumerate(real_crossovers, 1):
        c_type = "BULLISH" if cross['bullish'] else "BEARISH"
        print(f"{i}. {cross['symbol']} {cross['timeframe']} - {c_type}")
        print(f"   EMA9 (nến N-1): {cross['fast_prev']:.4f} | EMA21 (nến N-1): {cross['slow_prev']:.4f}")
        print(f"   EMA9 (nến N): {cross['fast_completed']:.4f} | EMA21 (nến N): {cross['slow_completed']:.4f}")

# Thống kê theo symbol/timeframe
print("\n=== BẢNG THỐNG KÊ TỔNG HỢP ===")
stats = {}
for cross in real_crossovers:
    key = (cross['symbol'], cross['timeframe'])
    if key not in stats:
        stats[key] = {'bullish':0, 'bearish':0}
    if cross['bullish']:
        stats[key]['bullish'] +=1
    if cross['bearish']:
        stats[key]['bearish'] +=1

print(f"{'Symbol':<12} | {'TF':<6} | {'Bullish':<7} | {'Bearish':<7} | {'Total':<5}")
print("-" * 55)
for (sym, tf), cnt in stats.items():
    total = cnt['bullish'] + cnt['bearish']
    print(f"{sym:<12} | {tf:<6} | {cnt['bullish']:<7} | {cnt['bearish']:<7} | {total:<5}")

# Tìm TOTAL_REAL_CROSSOVERS trong log (log của bot ghi lại)
print("\n=== KIỂM TRA LOG BOT GHI TỐNG SỐ CROSSOVER ===")
for line in lines:
    if 'TOTAL_REAL_CROSSOVERS' in line:
        print(line.strip())

# Kiểm tra no_finalized_crossover
no_finalized = sum(1 for line in lines if 'no_finalized_crossover' in line)
print(f"\nSố lần ghi no_finalized_crossover trong log: {no_finalized}")