"""
ff_tickers 全銘柄の長期日足を Yahoo Finance から取得し Supabase ff_prices_daily へ upsert する。

対象     : Supabase ff_tickers の全銘柄（security_type が STK / ETF）
期間     : period='max'（各銘柄の上場以来すべて）
価格調整 : auto_adjust=True（分割・配当調整済み。逆分割で破綻しない）
出力     : Supabase public.ff_prices_daily に直接 upsert
           （競合キー = (ticker, trade_date)、制約 uq_ff_prices_daily_ticker_date）

実行方法:
    cd C:\\projects\\stock-data\\daily_price_from_yahoofinance
    # 1) .env を用意（同梱の .env.example をコピーして service_role キーを記入）
    # 2) 依存をインストール
    pip install yfinance pandas supabase python-dotenv
    # 3) 実行
    python fetch_ff_tickers_daily.py

    動作確認の目安: yfinance 1.3.0 / pandas / supabase 2.x / Python 3.11+

注意:
    - ff_prices_daily は RLS 有効・ポリシー未設定のため anon キーでは書き込めない。
      service_role キー（RLS バイパス）を使う。キーは Supabase ダッシュボード
      Settings > API > service_role secret から取得し .env に置く（コミット禁止）。
    - ff_prices_daily は OHLCV のみ（adj_close 列なし）。auto_adjust=True のため
      close 等の OHLC 列が調整済み価格になる。volume は生の出来高。
    - 1銘柄が取得失敗・上場廃止でも全体は止めず、スキップしてログに残す。

変更履歴:
    v1: 初版（参考 QQQprj_fetch_yahoo.py の取得部を流用し、長format + Supabase upsert 化）
"""
import os
import sys
import time
import math
from pathlib import Path

import yfinance as yf
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

# ============================================================
# 設定
# ============================================================
TABLE_NAME = "ff_prices_daily"
TICKERS_TABLE = "ff_tickers"
PERIOD = "max"            # 取得可能な全期間
AUTO_ADJUST = True        # 分割・配当調整済み
ON_CONFLICT = "ticker,trade_date"
MAX_RETRIES = 3
RETRY_WAIT_SEC = 5
SLEEP_BETWEEN_TICKERS = 0.5   # レート制限回避の小休止（秒）
UPSERT_CHUNK = 1000           # 1リクエストあたりの行数

SCRIPT_DIR = Path(__file__).parent.resolve()


# ============================================================
# Supabase クライアント
# ============================================================
def get_supabase() -> Client:
    load_dotenv(SCRIPT_DIR / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: .env に SUPABASE_URL と SUPABASE_SERVICE_KEY を設定してください。")
        print(f"       期待する場所: {SCRIPT_DIR / '.env'}")
        sys.exit(1)
    return create_client(url, key)


def load_tickers(sb: Client) -> list[str]:
    """ff_tickers から対象シンボルを取得（STK / ETF）。"""
    rows = (
        sb.table(TICKERS_TABLE)
        .select("ticker, security_type")
        .execute()
        .data
    )
    tickers = sorted(
        {
            r["ticker"].strip().upper()
            for r in rows
            if r.get("ticker")
            and (r.get("security_type") in (None, "STK", "ETF"))
        }
    )
    return tickers


# ============================================================
# 取得
# ============================================================
def fetch_daily(ticker: str) -> pd.DataFrame | None:
    """1銘柄の長期日足を取得し ff_prices_daily の列に整形して返す。失敗は None。"""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.Ticker(ticker).history(
                period=PERIOD,
                auto_adjust=AUTO_ADJUST,
                actions=False,
            )
            if df is None or df.empty:
                raise ValueError("Empty DataFrame")

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            required = ["Open", "High", "Low", "Close", "Volume"]
            missing = [c for c in required if c not in df.columns]
            if missing:
                raise ValueError(f"Missing columns: {missing}")

            out = pd.DataFrame({
                "ticker": ticker,
                "trade_date": df.index.strftime("%Y-%m-%d"),
                "open": df["Open"].astype(float),
                "high": df["High"].astype(float),
                "low": df["Low"].astype(float),
                "close": df["Close"].astype(float),
                "volume": df["Volume"],
            })
            return out

        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_WAIT_SEC)

    print(f"    SKIP {ticker}: {type(last_error).__name__}: {last_error}")
    return None


def to_records(df: pd.DataFrame) -> list[dict]:
    """NaN を None に直し、volume を int 化して dict のリストへ。"""
    records = []
    for r in df.itertuples(index=False):
        def num(v):
            return None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)

        vol = r.volume
        vol = None if (vol is None or (isinstance(vol, float) and math.isnan(vol))) else int(vol)

        records.append({
            "ticker": r.ticker,
            "trade_date": r.trade_date,
            "open": num(r.open),
            "high": num(r.high),
            "low": num(r.low),
            "close": num(r.close),
            "volume": vol,
        })
    return records


def upsert_records(sb: Client, records: list[dict]) -> int:
    """チャンクに分けて upsert。書き込んだ行数を返す。"""
    total = 0
    for i in range(0, len(records), UPSERT_CHUNK):
        chunk = records[i:i + UPSERT_CHUNK]
        sb.table(TABLE_NAME).upsert(chunk, on_conflict=ON_CONFLICT).execute()
        total += len(chunk)
    return total


# ============================================================
# メイン
# ============================================================
def main():
    sb = get_supabase()

    print("=" * 60)
    print("ff_tickers 全銘柄 長期日足 取得 → ff_prices_daily upsert")
    print("=" * 60)

    tickers = load_tickers(sb)
    print(f"対象銘柄: {len(tickers)} 件 / period={PERIOD} / auto_adjust={AUTO_ADJUST}")
    print()

    ok, skipped, total_rows = 0, [], 0
    for idx, ticker in enumerate(tickers, 1):
        print(f"[{idx}/{len(tickers)}] {ticker} ...", end=" ", flush=True)
        df = fetch_daily(ticker)
        if df is None:
            skipped.append(ticker)
            print()
            time.sleep(SLEEP_BETWEEN_TICKERS)
            continue
        try:
            n = upsert_records(sb, to_records(df))
            ok += 1
            total_rows += n
            print(f"OK ({n} rows, {df['trade_date'].iloc[0]} ~ {df['trade_date'].iloc[-1]})")
        except Exception as e:
            skipped.append(ticker)
            print(f"UPSERT FAILED: {type(e).__name__}: {e}")
        time.sleep(SLEEP_BETWEEN_TICKERS)

    print()
    print("=" * 60)
    print("完了サマリー")
    print(f"  成功:   {ok} 銘柄 / {total_rows} 行 upsert")
    print(f"  スキップ: {len(skipped)} 銘柄")
    if skipped:
        print(f"  スキップ銘柄: {', '.join(skipped)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
