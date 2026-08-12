"""
ff_tickers 全銘柄の日足を marketdata.app から差分取得し ff_prices_daily へ upsert する。
（差分のみ: 各銘柄の max(trade_date)+1 〜 今日 を取得）

実行タイミング: 米国市場クローズ後に Claude Code スケジュールタスクが自動実行
トークン      : Supabase ff_config テーブル key='marketdata_token'
ログ          : ff_md_fetch_log へ記録

実行方法（手動）:
    cd C:\\projects\\stock-data\\daily_price_from_yahoofinance
    python fetch_ff_prices_daily_mda.py
    python fetch_ff_prices_daily_mda.py --date 2026-06-27  # 特定日を強制指定

依存パッケージ:
    pip install requests supabase python-dotenv

変更履歴:
    v1: 初版
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

# ============================================================
# 設定
# ============================================================
BASE = "https://api.marketdata.app/v1"
TICKERS_TABLE = "ff_tickers"
PRICES_TABLE = "ff_prices_daily"
LOG_TABLE = "ff_md_fetch_log"
SLEEP_BETWEEN_TICKERS = 0.3   # レート制限回避
UPSERT_CHUNK = 500

SCRIPT_DIR = Path(__file__).parent.resolve()


# ============================================================
# クライアント
# ============================================================
def get_supabase() -> Client:
    load_dotenv(SCRIPT_DIR / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: .env に SUPABASE_URL / SUPABASE_SERVICE_KEY が必要です。")
        sys.exit(1)
    return create_client(url, key)


def get_mda_token(sb: Client) -> str:
    row = sb.table("ff_config").select("value").eq("key", "marketdata_token").single().execute()
    if not row.data:
        print("ERROR: ff_config に marketdata_token が見つかりません。")
        sys.exit(1)
    return row.data["value"]


# ============================================================
# 差分日付計算
# ============================================================
def get_last_dates(sb: Client, tickers: list[str]) -> dict[str, str | None]:
    """各銘柄の最終取得日を一括で取得する。"""
    rows = (
        sb.table(PRICES_TABLE)
        .select("ticker, trade_date")
        .in_("ticker", tickers)
        .order("trade_date", desc=True)
        .execute()
        .data
    )
    last: dict[str, str | None] = {t: None for t in tickers}
    seen: set[str] = set()
    for r in rows:
        if r["ticker"] not in seen:
            last[r["ticker"]] = r["trade_date"]
            seen.add(r["ticker"])
    return last


# ============================================================
# marketdata.app 取得
# ============================================================
def fetch_candles(ticker: str, from_date: str, to_date: str, token: str) -> list[dict] | None:
    """
    marketdata.app から日足を取得し ff_prices_daily 形式の dict リストを返す。
    データなし (no_data) は空リスト、エラーは None。
    """
    url = f"{BASE}/stocks/candles/D/{ticker}/"
    params = {
        "from": from_date,
        "to": to_date,
        "format": "json",
        "token": token,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429:
            print("RATE LIMIT, 60秒待機...")
            time.sleep(60)
            r = requests.get(url, params=params, timeout=30)

        j = r.json()
        if j.get("s") == "no_data":
            return []
        if j.get("s") != "ok":
            print(f"    WARN {ticker}: s={j.get('s')} status={r.status_code}")
            return None

        rows = []
        for i in range(len(j["t"])):
            d = str(date.fromtimestamp(j["t"][i]))
            rows.append({
                "ticker": ticker,
                "trade_date": d,
                "open": float(j["o"][i]) if j["o"][i] is not None else None,
                "high": float(j["h"][i]) if j["h"][i] is not None else None,
                "low":  float(j["l"][i]) if j["l"][i] is not None else None,
                "close": float(j["c"][i]) if j["c"][i] is not None else None,
                "volume": int(j["v"][i]) if j["v"][i] is not None else None,
            })
        return rows

    except Exception as e:
        print(f"    ERROR {ticker}: {type(e).__name__}: {e}")
        return None


def upsert_rows(sb: Client, rows: list[dict]) -> int:
    total = 0
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i:i + UPSERT_CHUNK]
        sb.table(PRICES_TABLE).upsert(chunk, on_conflict="ticker,trade_date").execute()
        total += len(chunk)
    return total


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="取得終端日 YYYY-MM-DD（省略で今日）")
    args = parser.parse_args()

    today_str = args.date or date.today().isoformat()
    sb = get_supabase()
    token = get_mda_token(sb)

    print("=" * 60)
    print("ff_prices_daily 差分更新（marketdata.app）")
    print(f"取得終端日: {today_str}")
    print("=" * 60)

    # 対象銘柄を取得
    tickers = sorted({
        r["ticker"].strip().upper()
        for r in sb.table(TICKERS_TABLE).select("ticker, security_type").execute().data
        if r.get("ticker") and r.get("security_type") in (None, "STK", "ETF")
    })
    print(f"対象銘柄: {len(tickers)} 件")

    # 各銘柄の最終日を一括取得（最大500件ずつ Supabase に投げる）
    last_dates: dict[str, str | None] = {}
    for i in range(0, len(tickers), 500):
        last_dates.update(get_last_dates(sb, tickers[i:i + 500]))

    # 既に today まで揃っている銘柄はスキップ
    to_update = {t: last for t, last in last_dates.items() if last != today_str}
    print(f"更新対象: {len(to_update)} 件（スキップ: {len(tickers) - len(to_update)} 件）")
    print()

    ok, skipped, no_data, total_rows = 0, [], 0, 0
    credits_used = 0

    for idx, (ticker, last_date) in enumerate(sorted(to_update.items()), 1):
        # from_date = 最終取得日の翌日（未取得なら 90日前をデフォルト）
        if last_date:
            from_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
        else:
            from_date = (date.today() - timedelta(days=90)).isoformat()

        if from_date > today_str:
            no_data += 1
            continue

        print(f"[{idx}/{len(to_update)}] {ticker} ({from_date} ~ {today_str}) ...", end=" ", flush=True)
        rows = fetch_candles(ticker, from_date, today_str, token)
        credits_used += 1
        time.sleep(SLEEP_BETWEEN_TICKERS)

        if rows is None:
            skipped.append(ticker)
            print()
            continue
        if not rows:
            no_data += 1
            print("no_data")
            continue

        try:
            n = upsert_rows(sb, rows)
            ok += 1
            total_rows += n
            print(f"OK ({n} rows)")
        except Exception as e:
            skipped.append(ticker)
            print(f"UPSERT FAILED: {e}")

    print()
    print("=" * 60)
    print("完了サマリー")
    print(f"  更新成功: {ok} 銘柄 / {total_rows} 行")
    print(f"  no_data:  {no_data} 銘柄（非営業日・上場廃止等）")
    print(f"  スキップ: {len(skipped)} 銘柄")
    print(f"  APIコール: {credits_used} 件")
    if skipped:
        print(f"  スキップ銘柄: {', '.join(skipped)}")
    print("=" * 60)

    # ff_md_fetch_log に記録
    sb.table(LOG_TABLE).insert({
        "function_name": "ff_prices_daily_update",
        "from_date": today_str,
        "to_date": today_str,
        "rows_inserted": total_rows,
        "credits_used": credits_used,
        "status": "ok" if not skipped else "partial_or_error",
        "message": f"ok={ok} no_data={no_data} skip={len(skipped)} rows={total_rows} credits={credits_used}",
    }).execute()


if __name__ == "__main__":
    main()
