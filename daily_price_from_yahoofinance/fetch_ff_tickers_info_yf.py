"""
ff_tickers の会社情報を Yahoo Finance から取得し、NULL の項目だけ upsert する。

対象カラム（既存値は上書きしない）:
  company_name     ← info['longName']
  sector           ← info['sector']
  key_person       ← info['companyOfficers'] → "氏名 (役職)" カンマ区切り

company_overview は対象外（2026-07-05 除外）。
  → Yahoo Finance Japan の日本語「特色」を fetch_ff_overview_yahoojp.py が担当する。
    yfinance の longBusinessSummary は英語で、翻訳AIを噛ませる必要があり非効率だったため。
competitive_moat / weakness は AI 分析が必要なため対象外（ff-ticker-company-info-fill が担当）。

実行方法:
    cd C:\\projects\\stock-data\\daily_price_from_yahoofinance
    python fetch_ff_tickers_info_yf.py
    python fetch_ff_tickers_info_yf.py --force   # NULL でなくても上書き

依存パッケージ:
    pip install yfinance supabase python-dotenv
"""
import argparse
import os
import sys
import time
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client, Client

TICKERS_TABLE  = "ff_tickers"
SLEEP_BETWEEN  = 0.5
MAX_OFFICERS   = 5   # key_person に入れる役員数上限

SCRIPT_DIR = Path(__file__).parent.resolve()


def get_supabase() -> Client:
    load_dotenv(SCRIPT_DIR / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: .env に SUPABASE_URL / SUPABASE_SERVICE_KEY が必要です。")
        sys.exit(1)
    return create_client(url, key)


def format_key_person(officers: list) -> str | None:
    if not officers:
        return None
    results = []
    for o in officers[:MAX_OFFICERS]:
        name  = o.get("name", "").strip()
        title = o.get("title", "").strip()
        if name:
            results.append(f"{name} ({title})" if title else name)
    return ", ".join(results) if results else None


def fetch_info(ticker: str) -> dict | None:
    try:
        info = yf.Ticker(ticker).info
        if not info or info.get("quoteType") is None:
            return None

        officers = info.get("companyOfficers") or []
        return {
            "company_name":     info.get("longName") or info.get("shortName"),
            "sector":           info.get("sector"),
            # company_overview は fetch_ff_overview_yahoojp.py（Yahoo JP・日本語）に一本化（2026-07-05）
            "key_person":       format_key_person(officers),
        }
    except Exception as e:
        print(f"    ERROR {ticker}: {type(e).__name__}: {e}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="既存値も上書きする")
    args = parser.parse_args()

    sb = get_supabase()

    print("=" * 60)
    print("ff_tickers 会社情報補填（Yahoo Finance）")
    print(f"モード: {'強制上書き' if args.force else 'NULL のみ埋める'}")
    print("=" * 60)

    rows = (
        sb.table(TICKERS_TABLE)
        .select("pk_ff_tickers_id, ticker, security_type, company_name, sector, company_overview, key_person")
        .execute()
        .data
    )

    # STK/ETF のみ対象（OPT 等は除く）
    rows = [r for r in rows if r.get("security_type") in (None, "STK", "ETF")]

    if not args.force:
        # 4項目すべて埋まっているものはスキップ
        targets = [
            r for r in rows
            if not (r["company_name"] and r["sector"] and r["key_person"])
        ]
    else:
        targets = rows

    print(f"全銘柄: {len(rows)} 件 / 更新対象: {len(targets)} 件")
    print()

    ok, no_data, failed = 0, 0, []

    for idx, row in enumerate(targets, 1):
        ticker = row["ticker"]
        print(f"[{idx}/{len(targets)}] {ticker} ...", end=" ", flush=True)

        info = fetch_info(ticker)
        time.sleep(SLEEP_BETWEEN)

        if info is None:
            no_data += 1
            print("no_data")
            continue

        # force=False のときは既存値を守る
        patch: dict = {}
        for col in ("company_name", "sector", "key_person"):
            new_val = info.get(col)
            if new_val is None:
                continue
            if args.force or not row.get(col):
                patch[col] = new_val

        if not patch:
            no_data += 1
            print("skip (全項目埋まり済み)")
            continue

        try:
            sb.table(TICKERS_TABLE).update(patch).eq("pk_ff_tickers_id", row["pk_ff_tickers_id"]).execute()
            ok += 1
            filled = ", ".join(patch.keys())
            print(f"OK ({filled})")
        except Exception as e:
            failed.append(ticker)
            print(f"UPDATE FAILED: {e}")

    print()
    print("=" * 60)
    print("完了サマリー")
    print(f"  更新成功: {ok} 銘柄")
    print(f"  スキップ: {no_data} 銘柄（データなし・埋まり済み）")
    print(f"  失敗:     {len(failed)} 銘柄")
    if failed:
        print(f"  失敗銘柄: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
