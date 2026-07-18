import { createClient } from "@supabase/supabase-js";

// Supabase個人PJ（rf_bt_テーブル群）。RLSによりkazuohoso@gmail.comのログインが必要。
const url = import.meta.env.VITE_SUPABASE_URL ?? "https://rlnokfjidvfgigwwrulh.supabase.co";
const key =
  import.meta.env.VITE_SUPABASE_ANON_KEY ??
  "sb_publishable_TMD02GJH5K2n7O7CugedWA_ZT_0Qmyn";

export const supabase = createClient(url, key);
