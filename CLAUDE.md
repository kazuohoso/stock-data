# stock-data — FF 日次株価取得

このリポジトリは、Kaz の個人ERP（`kazuohoso/erp`）向けに **米国株の日足を Yahoo Finance から取得し Supabase に入れる Python スクリプト群**の置き場。
`daily_price_from_yahoofinance/fetch_ff_prices_daily_yf.py` が本体（Windows タスクスケジューラ「FF_PricesDailyUpdate_YahooFinance」で毎日 05:30 MYT 自動実行）。

---

## ⛔ 着手・回答の前に必ず読む（最重要）

**Kaz のアプリ（erp / beacon / FF / Research / 価格まわり等）について質問に答える・実装する前に、まず Supabase 個人PJ `rlnokfjidvfgigwwrulh` の横断検索を引くこと。** コードの grep やテーブル定義だけから推測で組み立てるのは順序が逆。答えは大抵すでに記録されている。

```sql
-- キーワードで3正本（dev_specs / dev_work_log / dev_playbook）を横断検索
SELECT source, code, title, snippet FROM fn_search_dev_docs('価格推移');
-- 全体像
SELECT source, code, title, one_line FROM v_dev_docs_toc ORDER BY source, meta, sort_order;
-- ヒットした spec の本文
SELECT content_for_ai FROM dev_specs WHERE code = '<code>';
```

3ドキュメントの役割：
- **dev_specs**（AI向けSSOT・現在有効な仕様/ルール/決定）
- **dev_work_log**（時系列の作業ログ・「何を・なぜ・次に」）
- **dev_playbook**（人間向けの呼び名辞書・UIパターン虎の巻）

> 注意：ERP編集フック `erp-specs-gate.js` は「erp リポの**編集時**」しか発火しない。この stock-data リポでの作業や、単なる質問・閲覧では何もリマインドされない。だから**AI側が自分の起動時規律として上を引く**。

---

## データの流れ（要点）

- 取得元＝**Yahoo Finance（yfinance）**。TradingView ではない。
- 保存先＝Supabase `ff_prices_daily`（ticker, trade_date, OHLCV）。約145万行・1962〜。
- **日次更新は差分**：`v_ff_prices_latest` で各銘柄の最新日を読み、`last_date + 1 → 米国最新取引日` だけ取得して upsert。全履歴の再取得はしない（初回の全期間ロードは別スクリプト `fetch_ff_tickers_daily.py`／`period=max`）。
- 取得仕様・落とし穴の正本＝dev_playbook `DATA-01`（`end` は排他／レート制限0.3秒sleep／クラス株はハイフン `BRK-B`／PostgREST 1000行上限でリスト欠落 等）。

## 価格推移チャート（Research/Positions「価格推移」タブ）

- 描画＝**TradingView `lightweight-charts` v5**（OSS・Apache 2.0）。左下の TradingView ロゴは**帰属表示であり、データ供給元ではない**。
- 表示データ＝上記 `ff_prices_daily`（自前DB・yfinance由来）。
- 正本＝dev_specs `research_price_chart_tab` / dev_playbook `CHT-02`。実装＝erp 側 `src/components/price-chart-tab.tsx`。

---

## 記録規律（この作業でも守る）

手を動かしたら **dev_work_log** に記録し、恒久的な仕様変更・決定は **dev_specs** に反映する（正本は Supabase `rlnokfjidvfgigwwrulh`）。詳細＝dev_specs `dev_recording_discipline`。
