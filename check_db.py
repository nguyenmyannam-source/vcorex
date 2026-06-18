import sqlite3
import os

db_path = r'e:\PYTHON_OKX\vcorex_38PDL_12_06_ADX_THAN_NEN\data\vcorex.db'
print(f"Checking database: {db_path}")
print(f"File exists: {os.path.exists(db_path)}")
print(f"File size: {os.path.getsize(db_path) / 1024:.2f} KB")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()
cursor.execute('SELECT name FROM sqlite_master WHERE type="table"')
print("\nTables:")
for table in cursor.fetchall():
    print(f"- {table[0]}")
conn.close()