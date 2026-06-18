from collections import defaultdict

log_path = r'e:\PYTHON_OKX\vcorex_38PDL_12_06_ADX_THAN_NEN\logs\vcorex.log'
rejection_counts = defaultdict(int)
adx_values = []
body_pct_values = []

with open(log_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

for line in lines:
    if '[SIGNAL_REJECTED]' in line:
        # Trích xuất reason
        if 'reason=' in line:
            reason_part = line.split('reason=')[1]
            reason = reason_part.split(' ')[0]
            rejection_counts[reason] +=1
            
        # Trích xuất ADX nếu có
        if 'adx:' in line:
            adx_part = line.split("'adx': ")[1]
            adx = float(adx_part.split(',')[0])
            adx_values.append(adx)
        # Trích xuất body_pct nếu có
        if 'body_pct:' in line:
            body_part = line.split("'body_pct': ")[1]
            body_pct = float(body_part.split(',')[0])
            body_pct_values.append(body_pct)

# In kết quả
print("=== BẢNG THỐNG KÊ NGUYÊN NHÂN TỪ CHỐI TÍN HIỆU ===")
print(f"{'Nguyên nhân':<20} | {'Số lần':<6} | {'Tỷ lệ %':<6}")
print("-" * 50)
total = sum(rejection_counts.values())
for reason, count in sorted(rejection_counts.items(), key=lambda x: -x[1]):
    pct = (count / total) * 100 if total >0 else 0
    print(f"{reason:<20} | {count:<6} | {pct:.2f}%")

# Thống kê ADX
print("\n=== THỐNG KÊ GIÁ TRỊ ADX TẠI CÁC TÍN HIỆU BỊ TỪ CHỐI ===")
if adx_values:
    print(f"Tổng số tín hiệu bị từ chối do ADX yếu: {len(adx_values)}")
    print(f"Min ADX: {min(adx_values):.2f}")
    print(f"Max ADX: {max(adx_values):.2f}")
    print(f"Avg ADX: {sum(adx_values)/len(adx_values):.2f}")
    
    # Phân phối ADX
    bins = {'<10':0, '10-15':0, '15-20':0, '20-25':0, '>=25':0}
    for adx in adx_values:
        if adx <10: bins['<10'] +=1
        elif adx <15: bins['10-15'] +=1
        elif adx <20: bins['15-20'] +=1
        elif adx <25: bins['20-25'] +=1
        else: bins['>=25'] +=1
    
    print("\nPhân phối ADX của các tín hiệu bị từ chối:")
    for bin_name, cnt in bins.items():
        print(f"ADX {bin_name}: {cnt} lần")