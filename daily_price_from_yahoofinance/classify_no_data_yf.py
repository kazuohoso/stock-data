"""
P249: ff_tickers のうち日足が1日も無い銘柄（first_trade_date IS NULL）を Yahoo Finance で1回だけ再確認する。

判定（Kaz決定 2026-11-01「Yahoo に載ってなかったらもうダメ」）:
  - period=max の日足が1行でも返れば  yahoo_ok
  - 記号入り（. / 空白 等）は Yahoo 表記（BRK.B -> BRK-B）でも1回試す。そちらで取れたら alt_ok（ticker は改名しない）
  - どちらも空なら no_data

★Yahoo は拒否・スロットルでも「空」を返す（落とし穴 yfinance_empty_three_meanings / yahoo_throttle_returns_empty_not_error）。
  そのため一定間隔で陽性対照（AAPL）を撃ち、空なら即中断する＝取りこぼしを no_data に化けさせない。

出力: CSV（ticker, result, yahoo_symbol, first_date, rows）。DB は書かない（反映は SQL で dry-run 件数を出してから行う）。
使い方: python classify_no_data_yf.py [--out result.csv]
"""
import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv
from supabase import create_client

SCRIPT_DIR = Path(__file__).resolve().parent
PAGE = 1000
BATCH = 40


def candidates(sb):
    rows, start = [], 0
    while True:
        chunk = (sb.table("ff_tickers").select("ticker")
                 .is_("first_trade_date", "null").eq("fetch_enabled", True)
                 .range(start, start + PAGE - 1).execute().data)
        rows += [r["ticker"] for r in chunk]
        if len(chunk) < PAGE:
            return sorted(set(rows))
        start += PAGE


def alt_symbol(t: str):
    if re.search(r"[^A-Z0-9^]", t):
        a = t.replace(".", "-").replace("/", "-").replace(" ", "-").rstrip("-.:")
        return a if a and a != t else None
    return None


def probe(symbols):
    """symbols のうち日足が返ったものを {sym: (first_date, rows)} で返す。"""
    found = {}
    if not symbols:
        return found
    df = yf.download(symbols, period="max", interval="1d", group_by="ticker",
                     auto_adjust=False, progress=False, threads=True)
    # ★yf.download は429を銘柄ごとの「空」に飲み込む＝そのままだと no_data に化ける。エラー台帳を見て例外にする
    errs = " ".join(str(v) for v in getattr(yf.shared, "_ERRORS", {}).values())
    if "RateLimit" in errs or "Too Many Requests" in errs:
        raise RuntimeError("rate limited inside download")
    for s in symbols:
        try:
            sub = df[s] if len(symbols) > 1 else df
            sub = sub.dropna(how="all")
            if len(sub) > 0:
                found[s] = (str(sub.index[0].date()), len(sub))
        except KeyError:
            pass
    return found


def positive_control():
    try:
        return len(yf.Ticker("AAPL").history(period="5d")) > 0
    except Exception as e:  # YFRateLimitError 等＝不調扱い（待って再試行）
        print(f"  陽性対照で例外: {type(e).__name__}", flush=True)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SCRIPT_DIR / "classify_no_data_result.csv"))
    args = ap.parse_args()
    load_dotenv(SCRIPT_DIR / ".env")
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    cands = candidates(sb)
    print(f"候補 {len(cands)} 件")
    for w in range(6):
        if positive_control():
            break
        print(f"開始前の陽性対照が空。{120 * (w + 1)}s 待つ", flush=True)
        time.sleep(120 * (w + 1))
    else:
        sys.exit("陽性対照 AAPL が空＝Yahoo 不調。中断")

    results = {}
    i = 0
    while i < len(cands):
        part = cands[i:i + BATCH]
        batch_res = {}
        try:
            found = probe(part)
        except Exception as e:
            print(f"  probe 例外 {type(e).__name__}。90s 待って再試行", flush=True)
            time.sleep(90)
            continue
        for t in part:
            if t in found:
                batch_res[t] = ("yahoo_ok", t, *found[t])
        miss = [t for t in part if t not in found]
        alts = {alt_symbol(t): t for t in miss if alt_symbol(t)}
        if alts:
            f2 = probe(list(alts))
            for a, t in alts.items():
                if a in f2:
                    batch_res[t] = ("alt_ok", a, *f2[a])
        for t in part:
            batch_res.setdefault(t, ("no_data", alt_symbol(t) or "", "", 0))
        # 毎バッチ陽性対照。空ならこのバッチを捨てて待ってからやり直す（空振りを no_data にしない）
        if not positive_control():
            retries = getattr(main, "_r", 0) + 1
            main._r = retries
            if retries > 6:
                sys.exit(f"{i} 件目で陽性対照が6回連続空＝中断（結果は書かない）")
            print(f"  {i} 件目: 陽性対照が空。{90 * retries}s 待って再試行", flush=True)
            time.sleep(90 * retries)
            continue
        main._r = 0
        results.update(batch_res)
        i += BATCH
        if (i // BATCH) % 10 == 0:
            print(f"  {i}/{len(cands)}", flush=True)
        time.sleep(3.0)

    if not positive_control():
        sys.exit("最後の陽性対照が空＝結果を信用できない。中断")
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "result", "yahoo_symbol", "first_date", "rows"])
        for t in cands:
            w.writerow([t, *results[t]])
    from collections import Counter
    print(Counter(r[0] for r in results.values()))
    print(f"出力: {args.out}")


if __name__ == "__main__":
    main()
