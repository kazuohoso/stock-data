# RF バックテスト データ供給バッチ（`rf_bt_`）

親計画: inbox `code=rf_us_value_experiment_v1` ／ 指示書: inbox `code=rf_backtest_v1` (v3)
Supabase 個人PJ: `rlnokfjidvfgigwwrulh`

reformer21 手法（割安放置＋還元で下値担保・損切りしない）の米国株バックテスト。
**設計の核（v3）**: 「通過銘柄だけ保存」をやめ、**全ユニバース銘柄 × screen_date** について
G1〜G6 の *個別* 判定フラグ＋生値を保存する。「通過」は保存値ではなく、
アプリで選んだ条件の組み合わせから **表示時に導出** する。これにより条件のオン/オフを
即時シミュレーションできる。

## フェーズ

| Phase | 内容 | 状態 |
|---|---|---|
| **P1 スパイク** | 1四半期 × 10銘柄で EDGAR → 全ゲート個別判定 → DB投入 → Kaz確認 | 本コミット（`rf_bt_spike.py`＋コアテーブル） |
| P2 バックフィル | テーブル拡充→10年全量（全ユニバース） | 未 |
| P3 アプリ | FF Sim（6画面・条件トグル・シナリオ比較）→ Vercel | 未 |
| P4 自動化 | 月次＋日次リクエストランナー＋登録3点 | 未 |
| P5 検算と結論 | 独立検算・グリッド比較・フォワード移行推奨 | 未 |

## P1 スパイクの内容

`rf_bt_spike.py` — screen_date=2016-03-31、10銘柄（BCC/MLI/SCS/UFPI/PATK/CMC/WNC/GHC/ATKR/TPX）。

各銘柄について:
- **EDGAR** (`data.sec.gov` companyfacts XBRL) から point-in-time（`filed <= screen_date` の最新値）で
  ファンダを取得し、accession を来歴として保存。
- **yfinance** で entry（screen_date翌営業日終値）・月次サンプル・+1y/+2y/+3y ホライズンリターンを取得。
- **G1〜G6 を個別に判定**（既定閾値。NULL=判定不能）:
  - G1 割安: forward PER ≤ 15
  - G2 実質割安: EV/EBIT ≤ 10 または FCF利回り ≥ 6%
  - G3 下値担保: shareholder yield ≥ 4%
  - G4 成長: 予想EPS成長 ≥ 7%/年 かつ forward PEG ≤ 1.2（バリアントN=実績外挿）
  - G5 財務: ネットキャッシュ or NetDebt/EBITDA ≤ 1.0
  - G6 資本効率: ROA ≥ 3%（スパイクは一律。本番は業種別ベンチマーク相対）
- `rf_bt_screens` / `rf_bt_horizon_returns` / `rf_bt_price_samples` に投入。

## 実行環境についての重要事項

**このバッチはローカル Windows（`C:\projects\stock-data\sceening\rf_backtest\`）で実行する前提。**
EDGAR (`data.sec.gov`) と yfinance に直アクセスできる環境が必要。

> ⚠️ Claude Code の**クラウドセッションでは EDGAR がegressポリシーで遮断**されており（`data.sec.gov` が 403）、
> クラウド側で実データを投入することはできない。よって本スパイクの実データ投入は
> **Kaz のローカル環境で下記コマンドを1回実行**して行う。コードとスキーマはクラウド側で用意済み。

## 実行手順（ローカル）

```bash
cd C:\projects\stock-data\sceening\rf_backtest
pip install -r requirements.txt

set SUPABASE_URL=https://rlnokfjidvfgigwwrulh.supabase.co
set SUPABASE_SERVICE_KEY=<service_role キー>
set SEC_UA=rf-backtest kazuohoso@gmail.com

python rf_bt_spike.py --dry-run   # 判定だけ画面表示（DB投入なし）
python rf_bt_spike.py             # DB投入
```

投入後の確認（Supabase SQL）:

```sql
SELECT ticker, per, fwd_per, ev_ebit, fcf_yield, sh_yield, roa,
       g1_pass, g2_pass, g3_pass, g4_pass, g5_pass, g6_pass
FROM rf_bt_screens WHERE screen_date = '2016-03-31' ORDER BY ticker;

-- 例: G1・G3 だけONにしたときの通過銘柄（表示時導出のイメージ）
SELECT ticker FROM rf_bt_screens
WHERE screen_date='2016-03-31' AND g1_pass AND g3_pass;
```

## 作成済みテーブル（Supabase・RLS3点セット済み）

`rf_bt_runs` / `rf_bt_screens` / `rf_bt_raw_fundamentals` /
`rf_bt_price_samples` / `rf_bt_horizon_returns`

P2 で追加予定: `rf_bt_trades` / `rf_bt_equity` / `rf_bt_metrics` /
`rf_bt_scenarios` / `rf_bt_run_requests`。
