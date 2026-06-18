import sqlite3
import json
from datetime import datetime, timedelta

def run_audit():
    db = sqlite3.connect('data/vcorex.db')
    db.row_factory = sqlite3.Row
    
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    date_str = seven_days_ago.strftime('%Y-%m-%d %H:%M:%S')
    
    # Total signals
    cursor = db.execute("SELECT COUNT(*) as c FROM signals WHERE created_at >= ?", (date_str,))
    total_signals = cursor.fetchone()['c']
    
    # Executed
    cursor = db.execute("SELECT COUNT(*) as c FROM signals WHERE created_at >= ? AND executed = 1", (date_str,))
    executed = cursor.fetchone()['c']
    
    # Rejected
    cursor = db.execute("SELECT COUNT(*) as c FROM signals WHERE created_at >= ? AND executed = 0", (date_str,))
    rejected = cursor.fetchone()['c']
    
    # Group by reject reason
    cursor = db.execute("""
        SELECT rejected_reason, COUNT(*) as c 
        FROM signals 
        WHERE created_at >= ? AND executed = 0
        GROUP BY rejected_reason
    """, (date_str,))
    
    reasons = {}
    for row in cursor.fetchall():
        reason = row['rejected_reason']
        if not reason:
            reason = 'Unknown'
        reasons[reason] = row['c']
        
    print(f"Total signals: {total_signals}")
    print(f"Executed: {executed}")
    print(f"Rejected: {rejected}")
    print("\nReject Reasons:")
    for k, v in reasons.items():
        pct = (v / rejected * 100) if rejected > 0 else 0
        print(f"{k} | {v} | {pct:.2f}%")

if __name__ == '__main__':
    run_audit()
