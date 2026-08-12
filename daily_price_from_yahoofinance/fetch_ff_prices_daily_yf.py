"""
ff_tickers 全銘柄の日足を Yahoo Finance から差分取得し ff_prices_daily へ upsert する。
（差分のみ: 各銘柄の max(trade_date)+1 〜 今日 を取得）

用途      : 毎日の株価更新（米国市場クローズ後に Windows タスクスケジューラが自動実行）
価格調整  : auto_adjust=True（分割・配当調整済み）
ログ      : ff_md_fetch_log へ記録 ＋ fetch_log.txt（このフォルダ・5MB超で自動間引き）へも記録
            （2026-08-12: Windowsタスク側のstdoutは既定で捨てられ、失敗の痕跡が一切残っていなかった
             ＝これが8/8〜8/11の障害を4日間気づけなかった直接原因。ファイルログを必須化した）
並列化    : ThreadPoolExecutor（I/O待ちがほとんどなのでスレッドで十分・2026-08-12改修）
            旧版=8000銘柄超を1件ずつ逐次取得＝実測60分超でタスクスケジューラの
            ExecutionTimeLimit(1時間)に毎回強制終了されていた（失敗コード1）。
            新版は並列8ワーカー＋銘柄ごとリトライで数分〜十数分に短縮。

実行方法（手動）:
    cd C:\\projects\\stock-data\\daily_price_from_yahoofinance
    python fetch_ff_prices_daily_yf.py
    python fetch_ff_prices_daily_yf.py --date 2026-06-27  # 終端日を強制指定
    python fetch_ff_prices_daily_yf.py --workers 12        # 並列数を変える（既定8）

依存パッケージ:
    pip install yfinance pandas supabase python-dotenv

変更履歴:
    v1: 初版（fetch_ff_tickers_daily.py の全量版を差分取得版に改造）
    2026-07-19: get_last_dates を ticker IN() チャンク化（v_ff_prices_latest全件横断がstatement timeoutで失敗する問題の修正）
    v2 (2026-08-12): 並列化(ThreadPoolExecutor)・銘柄ごとリトライ・ファイルログ必須化・
                      タスクの ExecutionTimeLimit を1時間→3時間に緩和（fix_scheduled_task.ps1で実施）
"""
import argparse
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf
import pandas as pd
from dotenv import load_dotenv
from supabase import create_client, Client

# ============================================================
# 設定
# ============================================================
PRICES_TABLE  = "ff_prices_daily"
TICKERS_TABLE = "ff_tickers"
LOG_TABLE     = "ff_md_fetch_log"
UPSERT_CHUNK  = 500
DEFAULT_WORKERS = 3      # 2026-08-12: 8〜10並列でYahoo側レート制限(429 Too Many Requests)を誘発し
                          # 全銘柄が失敗する事故が発生。IP単位の制限なのでKaz指示により余裕を持って絞る。
MAX_RETRIES   = 4        # 1銘柄あたりの取得リトライ回数（RULE-49/50: 外部通信は必ずリトライ）
RETRY_BASE_SLEEP = 2.0   # 指数バックオフの基準秒数（レート制限以外の一過性エラー用）
RATE_LIMIT_COOLDOWN_SEC = 90   # 429検知時、全ワーカーで共有して一斉に待つ秒数。短いと解除前に叩き直して悪化する
PACE_DELAY_SEC = 0.35    # 429が出ていない平常時でも、1リクエストごとにこの間隔を空けてバーストを避ける
TICKER_CHUNK = 300  # v_ff_prices_latestはDISTINCT ON全銘柄横断のためフィルタ無しだとPostgRESTのstatement_timeout(8s)に達する。ticker IN()で絞ってチャンク化する（対象12,763件で発覚・dev_tool_tips記載パターン）。

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_FILE = SCRIPT_DIR / "fetch_log.txt"
LOCK_FILE = SCRIPT_DIR / "fetch_running.lock"
LOCK_STALE_SEC = 4 * 3600  # 4時間以上前のロックは前回異常終了の残骸とみなして上書きする

# 429を検知したら全スレッドがここを見て一斉に待つ（個別バックオフだと各スレッドがバラバラに
# リトライを続け、結果的にリクエストが途切れずレート制限が解除されない＝2026-08-12の実障害）
_rate_limit_lock = threading.Lock()
_rate_limit_until = 0.0

# 平常時もワーカー間で"全体として"一定間隔を空けるための共有ペースメーカー（トークンバケット代わり）。
# ワーカーが各自 PACE_DELAY_SEC だけ待つだけでは、N並列だと結局 N倍速でリクエストが飛んでしまう。
_pace_lock = threading.Lock()
_next_slot = 0.0


def wait_for_pace_slot():
    global _next_slot
    with _pace_lock:
        now = time.time()
        start = max(now, _next_slot)
        _next_slot = start + PACE_DELAY_SEC
    delay = start - now
    if delay > 0:
        time.sleep(delay)


def wait_if_rate_limited():
    with _rate_limit_lock:
        until = _rate_limit_until
    remaining = until - time.time()
    if remaining > 0:
        time.sleep(remaining)


def trigger_rate_limit_cooldown():
    global _rate_limit_until
    with _rate_limit_lock:
        _rate_limit_until = max(_rate_limit_until, time.time() + RATE_LIMIT_COOLDOWN_SEC)


def log_line(msg: str) -> None:
    """標準出力 ＋ ファイルログの両方に書く。
    Windowsタスクスケジューラはstdoutを既定で捨てるため、ファイルログが唯一の事後診断手段。"""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # ログ書き込み失敗でバッチ本体を止めない


def rotate_log_if_huge() -> None:
    """ログが大きくなりすぎたら（目安5MB）先頭を切り詰める。日次数百行なので十分先の話。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# ============================================================
# クライアント
# ============================================================
def get_supabase() -> Client:
    load_dotenv(SCRIPT_DIR / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log_line("ERROR: .env に SUPABASE_URL / SUPABASE_SERVICE_KEY が必要です。")
        sys.exit(1)
    return create_client(url, key)


# PostgREST/Supabase はデフォルトで1リクエスト最大1000行しか返さない。
# 明示的にページングしないと1000件目以降が無言で欠落する（銘柄漏れの原因になった）。
PAGE_SIZE = 1000


def select_all(sb: Client, table: str, columns: str) -> list[dict]:
    """.range() で全行を取り切る（1000行上限を回避）。3回までリトライ（RULE-49）。"""
    rows: list[dict] = []
    start = 0
    while True:
        chunk = None
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                chunk = (
                    sb.table(table)
                    .select(columns)
                    .range(start, start + PAGE_SIZE - 1)
                    .execute()
                    .data
                )
                break
            except Exception as e:
                last_err = e
                time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))
        if chunk is None:
            raise RuntimeError(f"select_all({table}) failed after {MAX_RETRIES} retries: {last_err}")
        rows.extend(chunk)
        if len(chunk) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


# ============================================================
# 差分日付計算
# ============================================================
def get_last_dates(sb: Client, tickers: list[str]) -> dict[str, str | None]:
    """各銘柄の最新 trade_date を返す。
    v_ff_prices_latest（銘柄ごと最新1行のビュー）を使うので、履歴全走査より圧倒的に軽い。
    ticker IN() でチャンクごとに絞って取得する（フィルタ無しの全件取得はDISTINCT ON全銘柄横断計算になり
    対象銘柄数が増えるとPostgRESTのstatement_timeoutで失敗する＝2026-07-19以降これで日次更新が停止していた）。
    各チャンクは3回までリトライ（RULE-49）。"""
    last: dict[str, str | None] = {t: None for t in tickers}
    for i in range(0, len(tickers), TICKER_CHUNK):
        chunk = tickers[i:i + TICKER_CHUNK]
        rows = None
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                rows = (
                    sb.table("v_ff_prices_latest")
                    .select("ticker, trade_date")
                    .in_("ticker", chunk)
                    .execute()
                    .data
                )
                break
            except Exception as e:
                last_err = e
                time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))
        if rows is None:
            raise RuntimeError(f"get_last_dates chunk failed after {MAX_RETRIES} retries: {last_err}")
        for r in rows:
            last[r["ticker"]] = r["trade_date"]
    return last


# ============================================================
# Yahoo Finance 取得（1銘柄・リトライ内蔵）
# ============================================================
def fetch_daily_yf(ticker: str, from_date: str, to_date: str) -> list[dict] | None:
    """
    Yahoo Finance から日足を取得し ff_prices_daily 形式の dict リストを返す。
    データなしは空リスト、リトライを使い切ってもエラーなら None。
    to_date は yfinance の end（exclusive）なので +1日する。
    """
    end = (date.fromisoformat(to_date) + timedelta(days=1)).isoformat()
    last_err = None
    for attempt in range(MAX_RETRIES):
        wait_if_rate_limited()
        wait_for_pace_slot()
        try:
            df = yf.Ticker(ticker).history(
                start=from_date,
                end=end,
                auto_adjust=True,
                actions=False,
            )
            if df is None or df.empty:
                return []

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)

            required = ["Open", "High", "Low", "Close", "Volume"]
            if any(c not in df.columns for c in required):
                return []

            def num(v):
                return None if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)

            rows = []
            for ts, row in df.iterrows():
                d = ts.strftime("%Y-%m-%d")
                vol = row["Volume"]
                rows.append({
                    "ticker":     ticker,
                    "trade_date": d,
                    "open":       num(row["Open"]),
                    "high":       num(row["High"]),
                    "low":        num(row["Low"]),
                    "close":      num(row["Close"]),
                    "volume":     None if (vol is None or (isinstance(vol, float) and math.isnan(vol))) else int(vol),
                })
            return rows

        except Exception as e:
            last_err = e
            is_rate_limit = "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e) or "Rate limited" in str(e)
            if is_rate_limit:
                # 個別スレッドが各自バックオフすると、他スレッドが途切れず叩き続けるためレート制限が解けない。
                # 全スレッド共有のクールダウンを一度だけ発火し、次のwait_if_rate_limited()で全員が揃って待つ。
                trigger_rate_limit_cooldown()
                wait_if_rate_limited()
            else:
                # レート制限以外（一過性ネットワークエラー・廃止銘柄等）は通常の指数バックオフ
                time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))

    log_line(f"    ERROR {ticker}: {type(last_err).__name__}: {last_err}")
    return None


def upsert_rows(sb: Client, rows: list[dict]) -> int:
    total = 0
    for i in range(0, len(rows), UPSERT_CHUNK):
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                sb.table(PRICES_TABLE).upsert(rows[i:i + UPSERT_CHUNK], on_conflict="ticker,trade_date").execute()
                break
            except Exception as e:
                last_err = e
                time.sleep(RETRY_BASE_SLEEP * (2 ** attempt))
        else:
            raise RuntimeError(f"upsert failed after {MAX_RETRIES} retries: {last_err}")
        total += len(rows[i:i + UPSERT_CHUNK])
    return total


def check_yahoo_reachable() -> bool:
    """本番ループに入る前に1銘柄だけ試し、Yahoo Financeに全く繋がらない状態
    （ネット断・Yahoo側障害等）を早期検知する。ここで弾かないと、繋がらない状態のまま
    数千銘柄×3リトライを律儀に回し、失敗が確定するまで数時間かかってしまう（2026-08-12改修）。"""
    try:
        df = yf.Ticker("AAPL").history(period="5d", auto_adjust=True, actions=False)
        return df is not None and not df.empty
    except Exception:
        return False


def acquire_lock_or_exit():
    """二重起動防止（2026-08-12: 手動実行とタスクスケジューラの自動リトライが同時に走り、
    Yahooへの同時リクエスト数が想定の3倍になってレート制限を悪化させた事故の再発防止）。
    4時間以上前のロックは前回の異常終了の残骸とみなし、上書きして続行する。"""
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age < LOCK_STALE_SEC:
            log_line(f"SKIP: 既に実行中です（ロックファイル経過 {age:.0f}秒 < {LOCK_STALE_SEC}秒）。二重起動を回避します。")
            sys.exit(0)
        log_line(f"WARN: 古いロック（{age:.0f}秒前）を検出。前回異常終了とみなし上書きします。")
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="取得終端日 YYYY-MM-DD（省略で今日）")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="並列ワーカー数（既定4）")
    args = parser.parse_args()

    acquire_lock_or_exit()
    try:
        run(args)
    finally:
        release_lock()


def run(args):
    rotate_log_if_huge()
    today_str = args.date or date.today().isoformat()
    sb = get_supabase()

    log_line("=" * 60)
    log_line(f"ff_prices_daily 差分更新（Yahoo Finance・並列={args.workers}）")
    log_line(f"取得終端日: {today_str}")
    log_line("=" * 60)

    # ネット接続断・Yahoo側障害の早期検知（RULE-49/50: 恒久失敗と一過性失敗の切り分け）。
    # ここで弾かず本番ループへ入ると、全銘柄が失敗するまで数時間かかってしまう。
    if not check_yahoo_reachable():
        log_line("ERROR: Yahoo Finance に接続できません（ネット未接続 or Yahoo側障害の可能性）。今回は中断します。")
        log_line("       翌日の差分取得で自動的に埋め合わせます（データの永久欠損にはなりません）。")
        try:
            sb.table(LOG_TABLE).insert({
                "function_name": "ff_prices_daily_update_yf",
                "from_date": today_str, "to_date": today_str, "rows_inserted": 0,
                "status": "error", "message": "Yahoo Finance unreachable at connectivity pre-check",
            }).execute()
        except Exception:
            pass  # Supabaseにも繋がらない完全ネット断の場合はログ書き込み自体も失敗して当然
        sys.exit(1)

    tickers = sorted({
        r["ticker"].strip().upper()
        for r in select_all(sb, TICKERS_TABLE, "ticker, security_type")
        if r.get("ticker") and r.get("security_type") in (None, "STK", "ETF")
    })
    log_line(f"対象銘柄: {len(tickers)} 件")

    last_dates = get_last_dates(sb, tickers)

    to_update = {t: last for t, last in last_dates.items() if last != today_str}
    skip_count = len(tickers) - len(to_update)
    log_line(f"更新対象: {len(to_update)} 件（本日取得済みスキップ: {skip_count} 件）")

    ok, failed, no_data, total_rows = 0, [], 0, 0
    start_ts = time.time()

    def job(ticker: str, last_date: str | None):
        if last_date:
            from_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
        else:
            from_date = (date.today() - timedelta(days=90)).isoformat()
        if from_date > today_str:
            return ticker, "no_data", None
        rows = fetch_daily_yf(ticker, from_date, today_str)
        if rows is None:
            return ticker, "failed", None
        if not rows:
            return ticker, "no_data", None
        return ticker, "ok", rows

    items = sorted(to_update.items())
    # 🔒 UPSERTは銘柄ごとに送らず、バッファに貯めて UPSERT_CHUNK 行ごとにまとめて送る（RULE-71）。
    # 以前はここで銘柄ごとに upsert_rows() を呼んでいたため、差分が1〜2行しかなくても
    # 1銘柄=1HTTP往復になり、4,290銘柄で4,290リクエストがAPIとDBを飽和させていた
    # （2026-08-13 00:00-01:35 MYT にERP全画面が数十秒〜1分フリーズした実害の原因）。
    # upsert_rows() 側は元から500行ずつ送る作りだったが、呼ぶ側が1銘柄ずつ渡していたため
    # そのチャンク分割が一度も効いていなかった。
    buf: list[dict] = []
    pending_tickers: list[str] = []

    def flush_buffer() -> None:
        """溜まった行をまとめて送る。1回の呼び出し＝最小限のHTTP往復。"""
        nonlocal buf, pending_tickers, ok, total_rows
        if not buf:
            return
        try:
            n = upsert_rows(sb, buf)
            ok += len(pending_tickers)
            total_rows += n
        except Exception as e:
            failed.extend(pending_tickers)
            log_line(f"    UPSERT FAILED ({len(pending_tickers)}銘柄まとめ): {e}")
        finally:
            buf = []
            pending_tickers = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futures = {ex.submit(job, t, last): t for t, last in items}
        done_n = 0
        for fut in as_completed(futures):
            ticker, status, rows = fut.result()
            done_n += 1
            if done_n % 500 == 0:
                log_line(f"  進捗 {done_n}/{len(items)}（{time.time()-start_ts:.0f}秒経過）")
            if status == "no_data":
                no_data += 1
            elif status == "failed":
                failed.append(ticker)
            else:
                buf.extend(rows)
                pending_tickers.append(ticker)
                if len(buf) >= UPSERT_CHUNK:
                    flush_buffer()
    flush_buffer()  # 端数を最後に送る（送り忘れ＝データ欠落になるので必ず実行する）

    elapsed = time.time() - start_ts
    log_line("=" * 60)
    log_line("完了サマリー")
    log_line(f"  所要時間: {elapsed:.0f}秒")
    log_line(f"  更新成功: {ok} 銘柄 / {total_rows} 行")
    log_line(f"  no_data:  {no_data} 銘柄（非営業日・上場廃止等）")
    log_line(f"  失敗:     {len(failed)} 銘柄")
    if failed:
        log_line(f"  失敗銘柄: {', '.join(failed)}")
    log_line("=" * 60)

    try:
        sb.table(LOG_TABLE).insert({
            "function_name": "ff_prices_daily_update_yf",
            "from_date":     today_str,
            "to_date":       today_str,
            "rows_inserted": total_rows,
            "status":        "ok" if not failed else "partial_or_error",
            "message":       f"ok={ok} no_data={no_data} fail={len(failed)} rows={total_rows} elapsed={elapsed:.0f}s workers={args.workers}",
        }).execute()
    except Exception as e:
        log_line(f"  ff_md_fetch_log 書き込み失敗: {e}")


if __name__ == "__main__":
    main()
