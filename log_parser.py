import json

log_file = "logs/vcorex.log"
crossovers = 0
candles = 0
signals = 0
rejects = {}

with open(log_file, "r", encoding="utf-8") as f:
    for line in f:
        if "candle" in line.lower() or "received new candle" in line.lower():
            candles += 1
        if "crossover" in line.lower():
            crossovers += 1
        if "signal_created" in line or "signal created" in line.lower():
            signals += 1
        if "reject" in line.lower():
            # Example log: "Signal rejected for BTC-USDT-SWAP, reason=weak_trend_adx"
            # Attempt to extract reason
            parts = line.split("reason=")
            if len(parts) > 1:
                reason = parts[1].split()[0].strip().strip(",\"'")
                rejects[reason] = rejects.get(reason, 0) + 1

print(f"Candles processed: {candles}")
print(f"Crossovers: {crossovers}")
print(f"Signals created: {signals}")
print(f"Rejects: {rejects}")
