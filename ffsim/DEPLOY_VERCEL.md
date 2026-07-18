# FF Sim を Vercel に公開する手順

このアプリ（`ffsim/`）を Vercel に上げて、常時開けるURLにする手順。

## 前提
- リポジトリ: `kazuohoso/stock-data`（このアプリは `ffsim/` サブフォルダ）
- ブランチ: 公開したいブランチ（例: `claude/rf-backtest-v1-work-0bm7nm` または main にマージ後）
- ログイン用の Supabase 認証ユーザー（kazuohoso@gmail.com）が必要（RLSで保護のため）

## 手順（Vercel 画面・数クリック）
1. https://vercel.com にログイン →「Add New… → Project」
2. GitHub の `kazuohoso/stock-data` を **Import**
3. 設定画面で:
   - **Root Directory** → `ffsim` を選択（← ここが最重要）
   - Framework Preset → **Vite**（自動検出されるはず）
   - Build Command / Output → `vercel.json` があるので自動（vite build / dist）
4. （任意）**Environment Variables** に以下を追加（未設定でもコード内の既定値で動きます）:
   - `VITE_SUPABASE_URL` = `https://rlnokfjidvfgigwwrulh.supabase.co`
   - `VITE_SUPABASE_ANON_KEY` = `sb_publishable_TMD02GJH5K2n7O7CugedWA_ZT_0Qmyn`
5. **Deploy** を押す → 数十秒で `https://<プロジェクト名>.vercel.app` が発行される

## 公開後
- 以後、このブランチに git push するたびに自動で再ビルド・再公開されます。
- Supabase 側は変更不要（アプリが anon キー＋RLS で読み取り、ログインした kazuohoso@gmail.com のみデータ表示）。

## Supabase 認証ユーザーについて
開くとログイン画面が出ます。ユーザー未登録の場合は Supabase ダッシュボード →
Authentication → Users → Add user で kazuohoso@gmail.com のパスワードユーザーを作成してください。
（この作業は代行可能です。）
