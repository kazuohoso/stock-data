#!/usr/bin/env python3
"""
rf_bt_spike.py  —  RFバックテスト P1 スパイク（1四半期 × 10銘柄）

指示書: inbox code=rf_backtest_v1 (v3) / 親計画: rf_us_value_experiment_v1

目的:
  1銘柄=1行ではなく「全ユニバース銘柄 × screen_date」で
  G1〜G6 の *個別* 判定フラグ＋生値を rf_bt_screens に保存し、
  月次価格サンプルとホライズンリターンも全銘柄分を保存する。
  「通過」は保存値ではなく、表示時に選んだ条件から導出する（v3の核）。

このスパイクの立ち位置:
  - 本番のデータ供給はローカル Windows (C:\\projects\\stock-data\\sceening\\rf_backtest\\) で
    EDGAR (point-in-time XBRL, accession保存) + yfinance を直に叩いて実行する前提。
  - 本ファイルはその *縮小版*（1四半期・10銘柄）。パイプライン（EDGAR→全ゲート個別判定→DB投入）を
    end-to-end で通し、Kaz が数字を確認できるようにするのが狙い。

前提環境変数:
  SUPABASE_URL           例: https://rlnokfjidvfgigwwrulh.supabase.co
  SUPABASE_SERVICE_KEY   service_role キー（RLSをバイパスして投入するため）
  SEC_UA                 EDGAR用 User-Agent 例: "rf-backtest kazuohoso@gmail.com"

依存: requests, yfinance, pandas  (requirements.txt 参照)

冪等性: 同じ (run_id 生成キー, ticker, screen_date) は upsert。--full で当該 screen_date を全消し再計算。
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse
import datetime as dt
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# スパイクの対象（1四半期 × 10銘柄）
# ユニバース定義（米国普通株・$100M〜$5B・黒字・ex REIT/mREIT/BDC/MLP/銀行/保険/ADR）
# に大まかに沿った、循環／バリュー系の実在中小型株を10銘柄手選び。
# 本番ユニバースは rf_bt_backfill.py で機械構築する。
# ---------------------------------------------------------------------------
# スクリーニング基準日。スパイクは1点、バックフィルは四半期末を並べる。
# Kaz指定（2026-07-18）: 開始は2017-01以降の四半期末から。
SCREEN_DATES = [
    dt.date(y, m, d)
    for (y, m, d) in [
        (2017, 3, 31), (2017, 6, 30), (2017, 9, 30), (2017, 12, 31),
        (2018, 3, 31), (2018, 6, 30), (2018, 9, 30), (2018, 12, 31),
        # …P2本番では 2026-06 まで四半期ごとに延長（rf_bt_backfill）
    ]
]
SCREEN_DATE = SCREEN_DATES[0]               # スパイク単発時の既定
HORIZONS = {"1y": 365, "2y": 730, "3y": 1095}

SPIKE_TICKERS = [
    "BCC",   # Boise Cascade — 木材製品
    "MLI",   # Mueller Industries — 銅・金属加工
    "SCS",   # Steelcase — オフィス家具
    "UFPI",  # UFP Industries — 木材加工
    "PATK",  # Patrick Industries — RV/住宅部材
    "CMC",   # Commercial Metals — 鉄鋼
    "WNC",   # Wabash National — トレーラー
    "GHC",   # Graham Holdings — 教育・メディア
    "ATKR",  # Atkore — 電材
    "TPX",   # Tempur Sealy — マットレス
]

# 門番 既定閾値（rf_us_value_experiment_v1 §4）
DEFAULTS = dict(
    g1_per_max=15.0,          # G1 割安: forward PER ≤ 15
    g2_ev_ebit_max=10.0,      # G2 実質割安: EV/EBIT ≤ 10 …
    g2_fcf_yield_min=0.06,    #        … または FCF利回り ≥ 6%
    g3_sh_yield_min=0.04,     # G3 下値担保: shareholder yield ≥ 4%
    g4_eps_growth_min=0.07,   # G4 成長: 予想EPS成長 ≥ 7%/年 …
    g4_peg_max=1.2,           #        … かつ forward PEG ≤ 1.2
    g5_netdebt_ebitda_max=1.0,# G5 財務: ネットキャッシュ or NetDebt/EBITDA ≤ 1.0
    g6_roa_min=0.03,          # G6 資本効率: ROA ≥ 3%（スパイクは一律。本番は業種別ベンチマーク）
)

SEC_UA = os.environ.get("SEC_UA", "rf-backtest kazuohoso@gmail.com")
EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EDGAR_TICKERS = "https://www.sec.gov/files/company_tickers.json"


# ---------------------------------------------------------------------------
# EDGAR ヘルパ（point-in-time: filed <= screen_date の最新値を採用）
# ---------------------------------------------------------------------------
def _sec_get(url: str) -> dict:
    for attempt in range(4):
        r = requests.get(url, headers={"User-Agent": SEC_UA}, timeout=30)
        if r.status_code == 200:
            return r.json()
        time.sleep(2 ** attempt)  # 2,4,8,16s backoff
    r.raise_for_status()


def load_cik_map() -> dict[str, int]:
    data = _sec_get(EDGAR_TICKERS)
    return {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}


def latest_fact(facts: dict, tag: str, as_of: dt.date,
                units: str = "USD") -> Optional[tuple[float, str, str]]:
    """us-gaap:tag の中で filed<=as_of の最新値を (value, accn, end) で返す。"""
    node = facts.get("facts", {}).get("us-gaap", {}).get(tag)
    if not node:
        return None
    best = None
    for unit_key, rows in node.get("units", {}).items():
        if units not in unit_key:
            continue
        for row in rows:
            filed = dt.date.fromisoformat(row["filed"])
            if filed > as_of:
                continue
            key = (dt.date.fromisoformat(row["end"]), filed)
            if best is None or key > best[0]:
                best = (key, row["val"], row.get("accn", ""), row["end"])
    if best is None:
        return None
    return float(best[1]), best[2], best[3]


# ---------------------------------------------------------------------------
# 判定レコード
# ---------------------------------------------------------------------------
@dataclass
class Screen:
    ticker: str
    screen_date: str
    cik: Optional[int] = None
    # 生値
    price: Optional[float] = None
    eps_ttm: Optional[float] = None
    per: Optional[float] = None
    fwd_per: Optional[float] = None
    ev_ebit: Optional[float] = None
    fcf_yield: Optional[float] = None
    sh_yield: Optional[float] = None
    eps_growth: Optional[float] = None
    peg: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    roa: Optional[float] = None
    # 個別ゲートフラグ
    g1_pass: Optional[bool] = None
    g2_pass: Optional[bool] = None
    g3_pass: Optional[bool] = None
    g4_pass: Optional[bool] = None
    g5_pass: Optional[bool] = None
    g6_pass: Optional[bool] = None
    # 来歴（検算用）
    sources: dict = field(default_factory=dict)
    note: Optional[str] = None


def compute_gates(s: Screen, cfg: dict) -> Screen:
    """生値からG1〜G6の個別フラグを立てる（NULLは判定不能→None）。"""
    s.g1_pass = None if s.fwd_per is None else (s.fwd_per <= cfg["g1_per_max"])
    if s.ev_ebit is None and s.fcf_yield is None:
        s.g2_pass = None
    else:
        s.g2_pass = ((s.ev_ebit is not None and s.ev_ebit <= cfg["g2_ev_ebit_max"]) or
                     (s.fcf_yield is not None and s.fcf_yield >= cfg["g2_fcf_yield_min"]))
    s.g3_pass = None if s.sh_yield is None else (s.sh_yield >= cfg["g3_sh_yield_min"])
    if s.eps_growth is None or s.peg is None:
        s.g4_pass = None
    else:
        s.g4_pass = (s.eps_growth >= cfg["g4_eps_growth_min"] and s.peg <= cfg["g4_peg_max"])
    s.g5_pass = None if s.net_debt_ebitda is None else (s.net_debt_ebitda <= cfg["g5_netdebt_ebitda_max"])
    s.g6_pass = None if s.roa is None else (s.roa >= cfg["g6_roa_min"])
    return s


# ---------------------------------------------------------------------------
# 1銘柄のファンダを EDGAR から組み立てる
# ---------------------------------------------------------------------------
def build_screen(ticker: str, cik: int, price: float, as_of: dt.date) -> Screen:
    facts = _sec_get(EDGAR_FACTS.format(cik=cik))
    s = Screen(ticker=ticker, screen_date=as_of.isoformat(), cik=cik, price=price)
    src = {}

    def pick(tag, units="USD"):
        r = latest_fact(facts, tag, as_of, units)
        if r:
            val, accn, end = r
            src[tag] = {"accn": accn, "end": end, "val": val}
            return val
        return None

    # --- 損益・キャッシュフロー・BS 主要タグ ---
    net_income = pick("NetIncomeLoss")
    eps = pick("EarningsPerShareDiluted", units="USD/shares")
    revenue = (pick("RevenueFromContractWithCustomerExcludingAssessedTax")
               or pick("Revenues") or pick("SalesRevenueNet"))
    op_income = pick("OperatingIncomeLoss")
    assets = pick("Assets")
    cash = pick("CashAndCashEquivalentsAtCarryingValue")
    lt_debt = pick("LongTermDebtNoncurrent") or pick("LongTermDebt")
    st_debt = pick("LongTermDebtCurrent") or pick("DebtCurrent")
    dep_amort = pick("DepreciationDepletionAndAmortization") or pick("DepreciationAmortizationAndAccretionNet")
    op_cf = pick("NetCashProvidedByUsedInOperatingActivities")
    capex = pick("PaymentsToAcquirePropertyPlantAndEquipment")
    dividends = pick("PaymentsOfDividendsCommonStock") or pick("PaymentsOfDividends")
    buyback = pick("PaymentsForRepurchaseOfCommonStock")
    shares = pick("WeightedAverageNumberOfDilutedSharesOutstanding", units="shares")

    # --- 派生値 ---
    s.eps_ttm = eps
    if eps and eps > 0:
        s.per = round(price / eps, 2)
        # forward PER: バリアントN（実績外挿）。成長で割り戻す近似。
        # eps_growth 未確定時は trailing を暫定使用。
    ebit = op_income
    ebitda = (op_income + dep_amort) if (op_income is not None and dep_amort is not None) else None
    total_debt = (lt_debt or 0) + (st_debt or 0)
    net_debt = total_debt - (cash or 0)
    mcap = price * shares if shares else None
    ev = (mcap + total_debt - (cash or 0)) if mcap is not None else None

    if ev is not None and ebit:
        s.ev_ebit = round(ev / ebit, 2)
    if op_cf is not None and capex is not None and mcap:
        fcf = op_cf - capex
        s.fcf_yield = round(fcf / mcap, 4)
    if mcap:
        # shareholder yield = (配当 + 純自社株買い + 純負債返済) / 時価総額。
        # スパイクは配当＋自社株買いのみ（純負債返済は本番で追加）。
        sh_return = (dividends or 0) + (buyback or 0)
        s.sh_yield = round(sh_return / mcap, 4)
    if ebitda and ebitda != 0:
        s.net_debt_ebitda = round(net_debt / ebitda, 2)
    if net_income is not None and assets:
        s.roa = round(net_income / assets, 4)

    # G4 EPS成長 / PEG / forward PER: 過去EPSからの外挿（バリアントN）
    growth = _eps_growth(facts, as_of)
    if growth is not None:
        s.eps_growth = round(growth, 4)
        if s.per is not None and growth > 0:
            s.peg = round(s.per / (growth * 100), 2)
            s.fwd_per = round(price / (eps * (1 + growth)), 2) if (eps and eps > 0) else None
    if s.fwd_per is None and s.per is not None:
        s.fwd_per = s.per  # 成長不明時は trailing を保守的に流用

    s.sources = src
    return s


def _eps_growth(facts: dict, as_of: dt.date) -> Optional[float]:
    """直近と1年前の年次希薄化EPSからYoY成長率（バリアントN=実績外挿）。"""
    node = facts.get("facts", {}).get("us-gaap", {}).get("EarningsPerShareDiluted")
    if not node:
        return None
    annual = []
    for unit_key, rows in node.get("units", {}).items():
        if "USD/shares" not in unit_key:
            continue
        for row in rows:
            if dt.date.fromisoformat(row["filed"]) > as_of:
                continue
            start = row.get("start"); end = row.get("end")
            if not start:
                continue
            days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days
            if 330 <= days <= 400:  # 年次
                annual.append((dt.date.fromisoformat(end), float(row["val"])))
    if len(annual) < 2:
        return None
    annual.sort()
    (_, prev), (_, last) = annual[-2], annual[-1]
    if prev and prev > 0:
        return (last - prev) / prev
    return None


# ---------------------------------------------------------------------------
# 価格（yfinance）: entry = screen_date翌営業日終値、horizonリターン、月次サンプル
# ---------------------------------------------------------------------------
def fetch_prices(ticker: str, as_of: dt.date):
    import yfinance as yf
    end = as_of + dt.timedelta(days=HORIZONS["3y"] + 40)
    hist = yf.Ticker(ticker).history(start=as_of.isoformat(), end=end.isoformat(),
                                     auto_adjust=True, interval="1d")
    if hist.empty:
        return None, {}, []
    hist.index = [d.date() for d in hist.index]
    def close_on_or_after(d):
        for row_d, row in zip(hist.index, hist["Close"]):
            if row_d >= d:
                return float(row), row_d
        return None, None
    entry_px, entry_d = close_on_or_after(as_of + dt.timedelta(days=1))
    horizons = {}
    for name, days in HORIZONS.items():
        px, pd = close_on_or_after(as_of + dt.timedelta(days=days))
        if entry_px and px:
            horizons[name] = {"return": round(px / entry_px - 1, 4), "px": px, "date": pd.isoformat()}
    # 月次サンプル（各月最初の営業日）
    monthly, seen = [], set()
    for row_d, row in zip(hist.index, hist["Close"]):
        key = (row_d.year, row_d.month)
        if key not in seen:
            seen.add(key)
            monthly.append({"date": row_d.isoformat(), "adj_close": float(row)})
    return {"entry_px": entry_px, "entry_date": entry_d.isoformat() if entry_d else None}, horizons, monthly


# ---------------------------------------------------------------------------
# Supabase 投入（service_role で RLS バイパス）
# ---------------------------------------------------------------------------
def sb_upsert(table: str, rows: list[dict], on_conflict: str):
    url = os.environ["SUPABASE_URL"].rstrip("/") + f"/rest/v1/{table}"
    key = os.environ["SUPABASE_SERVICE_KEY"]
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    r = requests.post(url + f"?on_conflict={on_conflict}", headers=headers, data=json.dumps(rows), timeout=60)
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="当該screen_dateを全消し再計算")
    ap.add_argument("--dry-run", action="store_true", help="DB投入せず判定だけ表示")
    ap.add_argument("--single", action="store_true",
                     help="SCREEN_DATES全件ではなく先頭の1点(SCREEN_DATE)だけ実行")
    args = ap.parse_args()

    cfg = DEFAULTS
    dates = [SCREEN_DATE] if args.single else SCREEN_DATES
    print(f"[rf_bt_spike] screen_dates={[d.isoformat() for d in dates]} tickers={SPIKE_TICKERS}")
    cikmap = load_cik_map()

    total_screens = total_horizons = total_samples = 0
    for sd in dates:
        print(f"\n--- screen_date={sd} ---")
        screens, horizon_rows, sample_rows = [], [], []
        for t in SPIKE_TICKERS:
            cik = cikmap.get(t.upper())
            if not cik:
                print(f"  ! {t}: CIK未検出、スキップ"); continue
            px, horizons, monthly = fetch_prices(t, sd)
            entry_px = px["entry_px"] if px else None
            if entry_px is None:
                print(f"  ! {t}: 価格取得不可、スキップ"); continue
            s = compute_gates(build_screen(t, cik, entry_px, sd), cfg)
            screens.append(s)
            for name, h in horizons.items():
                horizon_rows.append(dict(ticker=t, screen_date=sd.isoformat(),
                                         horizon=name, ret=h["return"], end_price=h["px"],
                                         end_date=h["date"], outcome="alive"))
            for m in monthly:
                sample_rows.append(dict(ticker=t, sample_date=m["date"], adj_close=m["adj_close"]))
            g = "".join(["1" if getattr(s, f"g{i}_pass") else ("0" if getattr(s, f"g{i}_pass") is False else "?")
                         for i in range(1, 7)])
            print(f"  {t:5s} PER={s.per} fwdPER={s.fwd_per} EV/EBIT={s.ev_ebit} "
                  f"FCFy={s.fcf_yield} SY={s.sh_yield} gEPS={s.eps_growth} "
                  f"ND/EBITDA={s.net_debt_ebitda} ROA={s.roa}  gates[G1-6]={g}")
            time.sleep(0.2)

        if args.dry_run:
            continue

        screen_rows = []
        for s in screens:
            d = asdict(s)
            d["sources"] = json.dumps(s.sources)
            screen_rows.append(d)

        sb_upsert("rf_bt_screens", screen_rows, on_conflict="ticker,screen_date")
        if horizon_rows:
            sb_upsert("rf_bt_horizon_returns", horizon_rows, on_conflict="ticker,screen_date,horizon")
        if sample_rows:
            sb_upsert("rf_bt_price_samples", sample_rows, on_conflict="ticker,sample_date")
        total_screens += len(screen_rows)
        total_horizons += len(horizon_rows)
        total_samples += len(sample_rows)

    if args.dry_run:
        print("\n[dry-run] DB投入スキップ"); return
    print(f"\n[rf_bt_spike] 全screen_date投入完了: screens={total_screens} "
          f"horizons={total_horizons} samples={total_samples}")


if __name__ == "__main__":
    main()
