import sqlite3
from datetime import datetime

db_path = r'e:\PYTHON_OKX\vcorex_38PDL_12_06_ADX_THAN_NEN\data\vcorex.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get total signals
cursor.execute('SELECT COUNT(*) FROM signals')
print(f"Tổng số signal trong database: {cursor.fetchone()[0]}")

# Get schema of signals table
cursor.execute('PRAGMA table_info(signals)')
print("\nSchema bảng signals:")
for col in cursor.fetchall():
    print(f"  {col[1]}: {col[2]}")

# Get all signals with details
print("\n=== TẤT CẢ SIGNALS TRONG DATABASE (30 gần nhất) ===")
cursor.execute('''
SELECT created_at, symbol, timeframe, signal_type, 
       ema9_fast_prev, ema21_slow_prev, ema9_fast_current, ema21_slow_current,
       rejection_reason, is_accepted
FROM signals 
ORDER BY created_at DESC LIMIT 30
''')

signals = cursor.fetchall()
for sig in signals:
    created_at = datetime.fromtimestamp(sig[0]/1000) if sig[0] else "N/A"
    print(f"\n[{created_at}] {sig[1]} {sig[2]}")
    print(f"  Type: {sig[3]} | Accepted: {sig[9]}")
    print(f"  EMA prev: fast={sig[4]:.2f}, slow={sig[5]:.2f}")
    print(f"  EMA current: fast={sig[6]:.2f}, slow={sig[7]:.2f}")
    if sig[8]:
        print(f"  Lý do reject: {sig[8]}")
    
    # Tính toán điều kiện crossover
    if sig[4] is not None and sig[5] is not None and sig[6] is not None and sig[7] is not None:
        bullish = sig[4] <= sig[5] and sig[6] > sig[7]
        bearish = sig[4] >= sig[5] and sig[6] < sig[7]
        print(f"  Bullish condition: {bullish} | Bearish condition: {bearish}")

# Thống kê rejection reasons
print("\n=== THỐNG KÊ LÝ DO REJECT ===")
cursor.execute('''
SELECT rejection_reason, COUNT(*) as count 
FROM signals 
WHERE rejection_reason IS NOT NULL 
GROUP BY rejection_reason 
ORDER BY count DESC
''')
for row in cursor.fetchall():
    print(f"{row[0]}: {row[1]}")

# Đếm real crossovers - những tín hiệu có bullish hoặc bearish condition true
print("\n=== ĐẾM CROSSOVER THỰC TẾ ===")
cursor.execute('''
SELECT ema9_fast_prev, ema21_slow_prev, ema9_fast_current, ema21_slow_current, symbol, timeframe, created_at
FROM signals
WHERE ema9_fast_prev IS NOT NULL AND ema21_slow_prev IS NOT NULL 
  AND ema9_fast_current IS NOT NULL AND ema21_slow_current IS NOT NULL
''')
real_crossovers = 0
for row in cursor.fetchall():
    fast_prev, slow_prev, fast_curr, slow_curr = row[0], row[1], row[2], row[3]
    if (fast_prev <= slow_prev and fast_curr > slow_curr) or (fast_prev >= slow_prev and fast_curr < slow_curr):
        real_crossovers +=1
        print(f"Crossover thực tế: {row[4]} {row[5]} tại {datetime.fromtimestamp(row[6]/1000)}")

print(f"\nTổng số real crossovers: {real_crossovers}")

conn.close()