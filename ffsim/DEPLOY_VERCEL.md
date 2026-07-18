# FF Sim を Vercel に公開する手順

このアプリ（`ffsim/`）を Vercel に上げて、常時開けるURLにする手順。

## 前提
- リポジトリ: `kazuohoso/stock-data`（このアプリは `ffsim/` サブフォルダ）
- ブランチ: 公開したいブランチ（例: `claude/rf-backtest-v1-work-0bm7nm` または main にマージ後）
- 認証: dev_specs（decision_auth_supabase / auth_config）に従い **Google OAuth・オーナー限定
  （kazuohoso@gmail.com）**。ユーザーは登録済み（新規作成不要）。
- ⚠️ **Redirect URL の登録が必須**（登録しないと Site URL=kenko へ飛ばされる既知の罠）。

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

## Redirect URL 登録（ログイン成立に必須）
Personal プロジェクト（ref `rlnokfjidvfgigwwrulh`）の
Dashboard → Authentication → URL Configuration → Redirect URLs に、**既存を残したまま**追記:
- 本番: `https://<プロジェクト名>.vercel.app`（素の形）と `https://<プロジェクト名>.vercel.app/**` の両方
- ローカル: `http://localhost:5178` と `http://localhost:5178/**` の両方

※ Site URL（kenko）は変更しない。素の形と `/**` の両方を入れるのが定石（片方だけだと本番へ落ちる）。
Deploy 後にURLが確定するので、そのURLを上記に登録してから開くこと。
