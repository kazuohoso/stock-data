# 廃止: translate_ff_tickers_overview_ja.py

2026-07-05 廃止（`.DEPRECATED` にリネーム）。実行しないこと。

## なぜ廃止したか
- このスクリプトは yfinance の英語概要を **Anthropic API（Claude Haiku）で日本語に翻訳** していた。
- dev_specs `anthropic_api_usage_policy`（機械的に済むなら Anthropic API を噛ませない）に反していた。
- **代替**: `fetch_ff_overview_yahoojp.py` が Yahoo Finance Japan の日本語「特色」を直接取得するため、英語取得＋AI翻訳の2段が不要になった（課金・APIキーも不要）。

## company_overview の正規の担い手（現行）
- `fetch_ff_overview_yahoojp.py`（このフォルダ・非AI・Windowsタスク `ff_overview_yahoojp_fill` で週次）。

参照: 計画書 `C:\Obsidian\KazVault\取り組んでいること\FF会社概要自動補填\00_計画書.md`
