from dataclasses import dataclass, field

@dataclass
class TakeProfitLevel:
    price: float
    exit_pct: float

tp = [TakeProfitLevel(price=2800.0, exit_pct=0.5)]
tp_list = []
for t in tp:
    if isinstance(t, dict):
        tp_list.append(float(t.get('price', 0)))
    elif hasattr(t, 'price'):
        tp_list.append(float(t.price))
    else:
        tp_list.append(float(t))

print(" | ".join([f"${t:,.4f}" for t in tp_list[:3]]))
