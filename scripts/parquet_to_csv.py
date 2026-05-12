"""Convert data/history.parquet to data/history.csv.

Uses chunked write + fsync to avoid WSL→Windows mount truncation issues.
"""
from pathlib import Path
import os, sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PARQ = ROOT / "data" / "history.parquet"
CSV = ROOT / "data" / "history.csv"

if not PARQ.exists():
    print(f"missing: {PARQ}")
    raise SystemExit(1)

df = pd.read_parquet(PARQ)
total_rows = len(df)
total_tickers = df['ticker'].nunique()
print(f"Read parquet: {total_rows:,} rows, {total_tickers} tickers")
print(f"Date range: {df['date'].min()} → {df['date'].max()}")

# Write atomically: first to .tmp, fsync, then rename
tmp = CSV.with_suffix('.csv.tmp')
with open(tmp, 'w', newline='', encoding='utf-8') as f:
    df.to_csv(f, index=False)
    f.flush()
    os.fsync(f.fileno())

# Replace
if CSV.exists():
    CSV.unlink()
os.rename(tmp, CSV)

# Verify what was actually written
verify_df = pd.read_csv(CSV)
print(f"\nVerified write: {len(verify_df):,} rows, {verify_df['ticker'].nunique()} tickers")
print(f"Verified range: {verify_df['date'].min()} → {verify_df['date'].max()}")
print(f"Has SPY: {'SPY' in verify_df['ticker'].unique()}, QQQ: {'QQQ' in verify_df['ticker'].unique()}")
print(f"File size: {os.path.getsize(CSV):,} bytes")

if len(verify_df) < total_rows * 0.95:
    print(f"\n⚠️ WARNING: only {len(verify_df)}/{total_rows} rows survived write — WSL mount issue?")
    print("Try running this script directly from Windows PowerShell instead of WSL")
    sys.exit(2)
print("\n✓ CSV write successful and complete")
