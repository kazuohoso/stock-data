"""
ff_tickers.company_overview の空欄を Yahoo Finance Japan の日本語「特色」欄で埋める。

AIも翻訳も使わない純スクレイプ。Yahoo Finance Japan は米国株でも会社概要を
日本語で提供しており（ページHTMLの window.__PRELOADED_STATE__ に埋め込み）、
requests だけで取得できる（ブラウザ不要）。

対象:
  security_type = 'STK' かつ company_overview IS NULL の銘柄のみ。
  既存値は絶対に上書きしない（--force 指定時を除く）。

実行方法:
    cd C:\\projects\\stock-data\\daily_price_from_yahoofinance
    python fetch_ff_overview_yahoojp.py
    python fetch_ff_overview_yahoojp.py --limit 5      # 先頭5件だけ（検証用）
    python fetch_ff_overview_yahoojp.py --ticker AAPL  # 単一銘柄をDB更新せず表示だけ（動作確認）
    python fetch_ff_overview_yahoojp.py --force        # 既存値も上書き

依存パッケージ:
    pip install requests supabase python-dotenv
"""
import argparse
import json
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

TICKERS_TABLE = "ff_tickers"
LOG_TABLE     = "ff_md_fetch_log"
FUNC_NAME     = "ff_overview_yahoojp_fill"
BASE_URL      = "https://finance.yahoo.co.jp/quote/{ticker}/profile"
SLEEP_BETWEEN = 3.0     # 秒。基本間隔（±JITTER）。Yahoo JP のレート制限を避ける
JITTER        = 0.8     # 秒。間隔にランダムな揺らぎを加える
TIMEOUT       = 15
SECTION_TITLE = "特色"

# レート制限/一時ブロックとみなす HTTP ステータス。
# Yahoo JP は超過時に 500 の「ご覧になろうとしているページは現在表示できません」
# エラーページを返す（=データ無しではない。握り潰さず待機・再開する）。
BLOCK_STATUSES     = {429, 500, 502, 503}
BLOCK_PAGE_MARKERS = ("ご覧になろうとしている", "しばらく", "アクセスが集中")
MAX_BLOCK_RETRIES  = 5                        # 同一銘柄でブロックが続いたら諦めて中断
BACKOFF_SCHEDULE   = [30, 60, 120, 180, 300]  # ブロック時の待機（秒・指数的）

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

SCRIPT_DIR = Path(__file__).parent.resolve()


def get_supabase() -> Client:
    load_dotenv(SCRIPT_DIR / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: .env に SUPABASE_URL / SUPABASE_SERVICE_KEY が必要です。")
        sys.exit(1)
    return create_client(url, key)


# ---- 抽出ロジック（実データで検証済み） ----------------------------------

def extract_preloaded_state(html: str) -> dict | None:
    """HTML内の window.__PRELOADED_STATE__ = {...} の JSON を安全に取り出す。
    代入構文の揺れに強いよう、トークン直後の最初の { から raw_decode する。"""
    i = html.find("__PRELOADED_STATE__")
    if i < 0:
        return None
    b = html.find("{", i)
    if b < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[b:])
        return obj
    except json.JSONDecodeError:
        return None


def find_overview(state: dict) -> str | None:
    """パース済み state を再帰探索し、title=='特色' で items を持つ節から
    テキストを連結して返す。経路が変わっても拾えるよう構造でなく内容で探す。"""
    hits: list[str] = []

    def rec(o):
        if isinstance(o, dict):
            if o.get("title") == SECTION_TITLE and isinstance(o.get("items"), list):
                texts = [
                    it.get("text", "").strip()
                    for it in o["items"]
                    if isinstance(it, dict) and it.get("text")
                ]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    hits.append(joined)
            for v in o.values():
                rec(v)
        elif isinstance(o, list):
            for v in o:
                rec(v)

    rec(state)
    return hits[0] if hits else None


def fetch_overview(ticker: str) -> tuple[str, str | None]:
    """1銘柄の会社概要（日本語）を取得。(status, text) を返す。
      status: 'ok'     … text=概要
              'empty'  … ページはあるが特色なし／404（=真のデータ無し）
              'blocked'… レート制限/一時ブロック（待機・再開すべき）
              'error'  … 通信エラー等
    データ無しとブロックを区別するのが肝（旧版は 200 以外を一律 no_data にしていた）。"""
    url = BASE_URL.format(ticker=quote(ticker))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return ("error", None)

    if resp.status_code == 404:
        return ("empty", None)
    if resp.status_code in BLOCK_STATUSES:
        return ("blocked", None)
    if resp.status_code != 200:
        return ("error", None)

    html = resp.content.decode("utf-8", errors="replace")
    # 200 でもブロックページが返ることがある（防御的に判定）
    if "__PRELOADED_STATE__" not in html and any(m in html for m in BLOCK_PAGE_MARKERS):
        return ("blocked", None)

    state = extract_preloaded_state(html)
    if state is None:
        return ("empty", None)
    text = find_overview(state)
    return ("ok", text) if text else ("empty", None)


# ---- ログ ----------------------------------------------------------------

def write_log(sb: Client, status: str, rows: int, message: str):
    try:
        sb.table(LOG_TABLE).insert({
            "function_name": FUNC_NAME,
            "phase": "fill",
            "rows_inserted": rows,
            "status": status,
            "message": message,
        }).execute()
    except Exception as e:
        print(f"（ログ書き込み失敗: {e}）")


# ---- メイン --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="先頭N件だけ処理（検証用）")
    parser.add_argument("--force", action="store_true", help="既存値も上書きする")
    parser.add_argument("--ticker", type=str, default=None,
                        help="単一銘柄を取得して表示のみ（DB更新しない）")
    parser.add_argument("--sleep", type=float, default=SLEEP_BETWEEN,
                        help=f"銘柄間の基本待機秒（既定 {SLEEP_BETWEEN}）")
    args = parser.parse_args()

    # 単一銘柄の動作確認モード（DBに触らない）
    if args.ticker:
        status, ov = fetch_overview(args.ticker.upper())
        print(f"{args.ticker.upper()} [{status}]:")
        print(ov if ov else "（特色を取得できませんでした）")
        return

    sb = get_supabase()

    print("=" * 60)
    print("ff_tickers company_overview 補填（Yahoo Finance Japan・非AI）")
    print(f"モード: {'強制上書き' if args.force else 'NULL のみ埋める'}")
    print("=" * 60)

    q = sb.table(TICKERS_TABLE).select(
        "pk_ff_tickers_id, ticker, company_overview"
    ).eq("security_type", "STK")
    if not args.force:
        q = q.is_("company_overview", "null")
    rows = q.order("ticker").execute().data

    if args.limit:
        rows = rows[: args.limit]

    print(f"対象: {len(rows)} 件")
    print()

    ok, no_data, err, failed = 0, 0, 0, []
    aborted = False

    for idx, row in enumerate(rows, 1):
        ticker = row["ticker"]
        print(f"[{idx}/{len(rows)}] {ticker} ...", end=" ", flush=True)

        # force=False では既存値を絶対に触らない（二重ガード）
        if not args.force and row.get("company_overview"):
            print("skip (既存値あり)")
            continue

        # ブロック時は同一銘柄をバックオフ再試行。回復しなければ実行を中断する。
        status, overview = "error", None
        for attempt in range(MAX_BLOCK_RETRIES + 1):
            status, overview = fetch_overview(ticker)
            if status != "blocked":
                break
            if attempt < MAX_BLOCK_RETRIES:
                wait = BACKOFF_SCHEDULE[min(attempt, len(BACKOFF_SCHEDULE) - 1)]
                print(f"blocked→{wait}s待機", end=" ", flush=True)
                time.sleep(wait)

        if status == "blocked":
            # レート制限が解けない → 中断（NULLのみ対象なので次回実行で続きから埋まる）
            print("BLOCKED（中断）")
            aborted = True
            break

        if status == "ok":
            try:
                sb.table(TICKERS_TABLE).update(
                    {"company_overview": overview}
                ).eq("pk_ff_tickers_id", row["pk_ff_tickers_id"]).execute()
                ok += 1
                print(f"OK ({len(overview)} 文字)")
            except Exception as e:
                failed.append(ticker)
                print(f"UPDATE FAILED: {e}")
        elif status == "empty":
            no_data += 1
            print("no_data")
        else:  # error
            err += 1
            print("error")

        time.sleep(args.sleep + random.uniform(0, JITTER))

    print()
    print("=" * 60)
    print("完了サマリー" + ("（レート制限で中断）" if aborted else ""))
    print(f"  補填成功: {ok} 銘柄")
    print(f"  データなし: {no_data} 銘柄（Yahoo JPに特色なし・ページなし）")
    print(f"  通信エラー: {err} 銘柄")
    print(f"  更新失敗: {len(failed)} 銘柄")
    if failed:
        print(f"  失敗銘柄: {', '.join(failed)}")
    if aborted:
        print("  ※ Yahoo JP のレート制限で中断。NULLのみ対象なので次回実行で続きから埋まる。")
    print("=" * 60)

    status_log = "blocked" if aborted else ("partial" if (failed or err) else "ok")
    msg = f"補填{ok}/データなし{no_data}/エラー{err}/失敗{len(failed)}" + ("/中断(rate-limit)" if aborted else "")
    write_log(sb, status_log, ok, msg)

    # ASCII のセンチネル＋終了コードで結果を機械判定可能にする
    # （日本語をリダイレクトすると Windows では cp932 で書かれ、UTF-8 grep が一致しないため）
    print(f"RESULT={'ABORTED' if aborted else 'DONE'} filled={ok} no_data={no_data} err={err}")
    if aborted:
        sys.exit(2)   # レート制限で中断＝まだ残NULLあり（呼び出し側がクールダウン後に再実行）


if __name__ == "__main__":
    main()
