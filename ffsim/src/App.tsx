import { useEffect, useMemo, useState } from "react";
import { supabase } from "./lib/supabase";

/* ============ 型 ============ */
type Screen = {
  ticker: string; screen_date: string; price: number | null; eps_ttm: number | null;
  per: number | null; fwd_per: number | null; ev_ebit: number | null; fcf_yield: number | null;
  sh_yield: number | null; eps_growth: number | null; peg: number | null;
  net_debt_ebitda: number | null; roa: number | null;
  g1_pass: boolean | null; g2_pass: boolean | null; g3_pass: boolean | null;
  g4_pass: boolean | null; g5_pass: boolean | null; g6_pass: boolean | null;
  sources: Record<string, string> | null; note: string | null;
};
type Horizon = { ticker: string; screen_date: string; horizon: string; ret: number | null; entry_price: number | null; end_price: number | null; end_date: string | null; outcome: string };
type Sample = { ticker: string; sample_date: string; adj_close: number };
type Run = { phase: string; status: string; source_note: string | null; note: string | null; updated_at: string };
type Scenario = { pk_scenario_id?: string; name: string; toggles: Toggles };

/* ============ 条件セット（v3: 表示時導出の正本） ============ */
type Toggles = {
  g1: boolean; g2: boolean; g3: boolean; g4: boolean; g5: boolean; g6: boolean;
  g1_per_max: number;      // 12 / 15 / 20
  g3_sy_min: number;       // 0.03 / 0.04 / 0.05
};
const DEFAULT_TOGGLES: Toggles = { g1: true, g2: true, g3: true, g4: true, g5: true, g6: true, g1_per_max: 15, g3_sy_min: 0.04 };

/** 生値から選択条件でゲートを再判定（保存済みフラグではなく生値ベース。閾値プリセット変更に追従） */
function judge(s: Screen, t: Toggles) {
  const g1 = s.fwd_per == null ? null : s.fwd_per <= t.g1_per_max;
  const g2 = s.ev_ebit == null && s.fcf_yield == null ? null
    : (s.ev_ebit != null && s.ev_ebit <= 10) || (s.fcf_yield != null && s.fcf_yield >= 0.06);
  const g3 = s.sh_yield == null ? null : s.sh_yield >= t.g3_sy_min;
  const g4 = s.eps_growth == null || s.peg == null ? null : s.eps_growth >= 0.07 && s.peg <= 1.2;
  const g5 = s.net_debt_ebitda == null ? null : s.net_debt_ebitda <= 1.0;
  const g6 = s.roa == null ? null : s.roa >= 0.03;
  const gates = { g1, g2, g3, g4, g5, g6 } as const;
  const active = (Object.keys(gates) as (keyof typeof gates)[]).filter((k) => t[k]);
  const pass = active.every((k) => gates[k] === true);
  const judgeable = active.every((k) => gates[k] !== null);
  return { gates, pass: judgeable ? pass : false, judgeable };
}

function pct(v: number | null | undefined, digits = 1) {
  return v == null ? "—" : `${(v * 100).toFixed(digits)}%`;
}

/* ============ スパークライン（inline SVG） ============ */
function Spark({ points }: { points: number[] }) {
  if (points.length < 2) return <span className="text-gray-400">—</span>;
  const min = Math.min(...points), max = Math.max(...points), w = 120, h = 28;
  const xy = points.map((p, i) => `${(i / (points.length - 1)) * w},${h - ((p - min) / (max - min || 1)) * h}`);
  const up = points[points.length - 1] >= points[0];
  return (
    <svg width={w} height={h} className="inline-block align-middle">
      <polyline points={xy.join(" ")} fill="none" stroke={up ? "#059669" : "#dc2626"} strokeWidth="1.5" />
    </svg>
  );
}

/* ============ App ============ */
export default function App() {
  const [session, setSession] = useState<null | { email?: string }>(null);
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session ? { email: data.session.user.email } : null);
      setAuthChecked(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_e, s) =>
      setSession(s ? { email: s.user.email } : null)
    );
    return () => sub.subscription.unsubscribe();
  }, []);

  if (!authChecked) return <Center>読み込み中…</Center>;
  if (!session) return <Login />;
  return <Main email={session.email ?? ""} />;
}

function Center({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen flex items-center justify-center text-gray-500">{children}</div>;
}

function Login() {
  const [err, setErr] = useState("");
  // dev_specs: 全アプリの認証は Supabase Auth の Google OAuth・オーナー限定。
  // redirectTo は素の origin（localhost_port_map の書式の落とし穴に合わせる）。
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="bg-white p-8 rounded-xl shadow w-80 space-y-4 text-center">
        <h1 className="text-lg font-bold">FF Sim — RF Backtest</h1>
        <p className="text-sm text-gray-500">kazuohoso@gmail.com 限定</p>
        <button
          className="bg-gray-900 text-white rounded w-full p-2.5"
          onClick={async () => {
            const { error } = await supabase.auth.signInWithOAuth({
              provider: "google",
              options: { redirectTo: window.location.origin },
            });
            if (error) setErr(error.message);
          }}
        >
          Googleでログイン
        </button>
        {err && <div className="text-red-600 text-sm">{err}</div>}
      </div>
    </div>
  );
}

const TABS = ["ダッシュボード", "コホート", "条件シミュレータ", "検算"] as const;

function Main({ email }: { email: string }) {
  const [tab, setTab] = useState<(typeof TABS)[number]>("ダッシュボード");
  const [screens, setScreens] = useState<Screen[]>([]);
  const [horizons, setHorizons] = useState<Horizon[]>([]);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [toggles, setToggles] = useState<Toggles>(DEFAULT_TOGGLES);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [compare, setCompare] = useState<string[]>([]); // 選択シナリオ名 (最大3)

  useEffect(() => {
    (async () => {
      const [a, b, c, d, e] = await Promise.all([
        supabase.from("rf_bt_screens").select("*").order("ticker"),
        supabase.from("rf_bt_horizon_returns").select("*"),
        supabase.from("rf_bt_price_samples").select("ticker,sample_date,adj_close").order("sample_date"),
        supabase.from("rf_bt_runs").select("phase,status,source_note,note,updated_at").order("updated_at", { ascending: false }),
        supabase.from("rf_bt_scenarios").select("pk_scenario_id,name,toggles"),
      ]);
      setScreens((a.data as Screen[]) ?? []);
      setHorizons((b.data as Horizon[]) ?? []);
      setSamples((c.data as Sample[]) ?? []);
      setRuns((d.data as Run[]) ?? []);
      setScenarios((e.data as Scenario[]) ?? []);
    })();
  }, []);

  const hzMap = useMemo(() => {
    const m: Record<string, Record<string, number | null>> = {};
    for (const h of horizons) (m[h.ticker] ??= {})[h.horizon] = h.ret;
    return m;
  }, [horizons]);
  const sparkMap = useMemo(() => {
    const m: Record<string, number[]> = {};
    for (const s of samples) (m[s.ticker] ??= []).push(s.adj_close);
    return m;
  }, [samples]);

  const summarize = (t: Toggles) => {
    const cohort = screens.filter((s) => judge(s, t).pass);
    const win = (hz: string) => {
      const rets = cohort.map((s) => hzMap[s.ticker]?.[hz]).filter((r): r is number => r != null);
      return rets.length ? { n: rets.length, win: rets.filter((r) => r > 0).length / rets.length, avg: rets.reduce((a, b) => a + b, 0) / rets.length } : null;
    };
    return { cohort, w1: win("1y"), w2: win("2y"), w3: win("3y") };
  };
  const cur = useMemo(() => summarize(toggles), [screens, hzMap, toggles]);

  const freshness = runs[0]?.updated_at ? new Date(runs[0].updated_at) : null;
  const staleDays = freshness ? Math.floor((Date.now() - freshness.getTime()) / 86400000) : null;

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <header className="bg-gray-900 text-white px-6 py-3 flex items-center justify-between">
        <div className="font-bold">FF Sim <span className="font-normal text-gray-300 text-sm">RFバックテスト（P1スパイク: 2016-03-31 × 10銘柄）</span></div>
        <div className="text-sm flex items-center gap-4">
          {staleDays != null && (
            <span className={`px-2 py-0.5 rounded text-xs ${staleDays > 7 ? "bg-red-600" : "bg-emerald-600"}`}>
              データ鮮度 {staleDays}日前
            </span>
          )}
          <span className="text-gray-300">{email}</span>
          <button className="underline" onClick={() => supabase.auth.signOut()}>ログアウト</button>
        </div>
      </header>
      <nav className="bg-white border-b px-6 flex gap-1">
        {TABS.map((t) => (
          <button key={t} onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm border-b-2 ${tab === t ? "border-gray-900 font-semibold" : "border-transparent text-gray-500"}`}>
            {t}
          </button>
        ))}
      </nav>

      <main className="p-6 max-w-6xl mx-auto space-y-6">
        {tab === "ダッシュボード" && (
          <>
            <div className="grid grid-cols-4 gap-4">
              <Stat label="ユニバース銘柄" value={`${screens.length}`} />
              <Stat label="現条件の通過銘柄" value={`${cur.cohort.length}`} />
              <Stat label="1y勝率" value={cur.w1 ? pct(cur.w1.win, 0) : "—"} sub={cur.w1 ? `平均 ${pct(cur.w1.avg)}` : ""} />
              <Stat label="3y勝率" value={cur.w3 ? pct(cur.w3.win, 0) : "—"} sub={cur.w3 ? `平均 ${pct(cur.w3.avg)}` : ""} />
            </div>
            <div className="bg-amber-50 border border-amber-200 rounded p-4 text-sm text-amber-900">
              <b>バイアス注記（常設）</b>：P1スパイクはPolygon(SEC XBRL由来)＋IBKR価格による近似計算。SY=−財務CF、FCF=営業CF+投資CF、EV=時価総額+長期借入（現金控除なし）等の近似を含む。
              生存者バイアス・実績外挿（バリアントN）・配当調整残差あり。本番はローカルEDGAR直バッチで置換予定。ATKR=IPO前でデータ非存在、WNC/PATK=価格保留。
            </div>
            <div className="bg-white rounded-xl shadow p-4 text-sm">
              <div className="font-semibold mb-2">バッチ実行台帳（rf_bt_runs）</div>
              {runs.map((r, i) => (
                <div key={i} className="flex gap-3 border-t py-1.5 text-gray-700">
                  <span className="font-mono">{r.phase}</span>
                  <span className={r.status === "done" ? "text-emerald-600" : "text-amber-600"}>{r.status}</span>
                  <span className="text-gray-400 truncate">{r.source_note}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {tab === "コホート" && (
          <CohortTable screens={screens} toggles={toggles} hzMap={hzMap} sparkMap={sparkMap} />
        )}

        {tab === "条件シミュレータ" && (
          <>
            <TogglePanel toggles={toggles} setToggles={setToggles} />
            <ScenarioBar toggles={toggles} setToggles={setToggles} scenarios={scenarios} setScenarios={setScenarios} compare={compare} setCompare={setCompare} />
            <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${1 + compare.length}, minmax(0,1fr))` }}>
              <ScenarioCard title="現在の条件" data={summarize(toggles)} />
              {compare.map((name) => {
                const sc = scenarios.find((s) => s.name === name);
                return sc ? <ScenarioCard key={name} title={name} data={summarize({ ...DEFAULT_TOGGLES, ...sc.toggles })} /> : null;
              })}
            </div>
            <CohortTable screens={screens} toggles={toggles} hzMap={hzMap} sparkMap={sparkMap} />
            <RequestFullCalc toggles={toggles} />
          </>
        )}

        {tab === "検算" && <Audit screens={screens} />}
      </main>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-white rounded-xl shadow p-4">
      <div className="text-xs text-gray-500">{label}</div>
      <div className="text-2xl font-bold">{value}</div>
      {sub && <div className="text-xs text-gray-400">{sub}</div>}
    </div>
  );
}

function TogglePanel({ toggles, setToggles }: { toggles: Toggles; setToggles: (t: Toggles) => void }) {
  const gate = (k: keyof Toggles, label: string) => (
    <label className="flex items-center gap-1.5 text-sm bg-white rounded-lg shadow px-3 py-2 cursor-pointer">
      <input type="checkbox" checked={toggles[k] as boolean} onChange={(e) => setToggles({ ...toggles, [k]: e.target.checked })} />
      {label}
    </label>
  );
  return (
    <div className="flex flex-wrap items-center gap-2">
      {gate("g1", "G1 割安 (fwd PER)")}
      {gate("g2", "G2 実質割安 (EV/EBIT・FCF)")}
      {gate("g3", "G3 下値担保 (SY)")}
      {gate("g4", "G4 成長 (EPS・PEG)")}
      {gate("g5", "G5 財務 (NetDebt)")}
      {gate("g6", "G6 資本効率 (ROA)")}
      <span className="text-sm text-gray-500 ml-2">PER上限:</span>
      {[12, 15, 20].map((v) => (
        <button key={v} onClick={() => setToggles({ ...toggles, g1_per_max: v })}
          className={`px-2 py-1 rounded text-sm ${toggles.g1_per_max === v ? "bg-gray-900 text-white" : "bg-white shadow"}`}>{v}</button>
      ))}
      <span className="text-sm text-gray-500 ml-2">SY下限:</span>
      {[0.03, 0.04, 0.05].map((v) => (
        <button key={v} onClick={() => setToggles({ ...toggles, g3_sy_min: v })}
          className={`px-2 py-1 rounded text-sm ${toggles.g3_sy_min === v ? "bg-gray-900 text-white" : "bg-white shadow"}`}>{v * 100}%</button>
      ))}
    </div>
  );
}

function ScenarioBar(props: {
  toggles: Toggles; setToggles: (t: Toggles) => void;
  scenarios: Scenario[]; setScenarios: (s: Scenario[]) => void;
  compare: string[]; setCompare: (c: string[]) => void;
}) {
  const [name, setName] = useState("");
  return (
    <div className="bg-white rounded-xl shadow p-3 flex flex-wrap items-center gap-2 text-sm">
      <input className="border rounded p-1.5" placeholder="シナリオ名" value={name} onChange={(e) => setName(e.target.value)} />
      <button className="bg-gray-900 text-white rounded px-3 py-1.5"
        onClick={async () => {
          if (!name) return;
          const { error } = await supabase.from("rf_bt_scenarios").upsert({ name, toggles: props.toggles }, { onConflict: "name" });
          if (!error) {
            const others = props.scenarios.filter((s) => s.name !== name);
            props.setScenarios([...others, { name, toggles: props.toggles }]);
            setName("");
          }
        }}>
        現在の条件を保存
      </button>
      <span className="text-gray-400 mx-2">|</span>
      <span className="text-gray-500">比較 (最大3):</span>
      {props.scenarios.map((s) => (
        <button key={s.name}
          onClick={() => {
            const on = props.compare.includes(s.name);
            props.setCompare(on ? props.compare.filter((n) => n !== s.name) : [...props.compare, s.name].slice(-3));
          }}
          onDoubleClick={() => props.setToggles({ ...DEFAULT_TOGGLES, ...s.toggles })}
          className={`px-2 py-1 rounded ${props.compare.includes(s.name) ? "bg-emerald-600 text-white" : "bg-gray-100"}`}
          title="クリック=比較に追加 / ダブルクリック=この条件を読み込み">
          {s.name}
        </button>
      ))}
    </div>
  );
}

function ScenarioCard({ title, data }: { title: string; data: { cohort: Screen[]; w1: any; w2: any; w3: any } }) {
  const row = (label: string, w: { n: number; win: number; avg: number } | null) => (
    <tr className="border-t">
      <td className="py-1 text-gray-500">{label}</td>
      <td className="text-right">{w ? `${w.n}銘柄` : "—"}</td>
      <td className="text-right">{w ? pct(w.win, 0) : "—"}</td>
      <td className={`text-right ${w && w.avg >= 0 ? "text-emerald-600" : "text-red-600"}`}>{w ? pct(w.avg) : "—"}</td>
    </tr>
  );
  return (
    <div className="bg-white rounded-xl shadow p-4">
      <div className="font-semibold text-sm mb-1">{title}</div>
      <div className="text-2xl font-bold">{data.cohort.length}<span className="text-sm font-normal text-gray-500"> 銘柄通過</span></div>
      <div className="text-xs text-gray-500 mb-2">{data.cohort.map((s) => s.ticker).join(", ") || "該当なし"}</div>
      <table className="w-full text-xs">
        <thead><tr className="text-gray-400"><th></th><th className="text-right">n</th><th className="text-right">勝率</th><th className="text-right">平均</th></tr></thead>
        <tbody>{row("1年", data.w1)}{row("2年", data.w2)}{row("3年", data.w3)}</tbody>
      </table>
    </div>
  );
}

function CohortTable({ screens, toggles, hzMap, sparkMap }: {
  screens: Screen[]; toggles: Toggles;
  hzMap: Record<string, Record<string, number | null>>; sparkMap: Record<string, number[]>;
}) {
  const g = (v: boolean | null) => (v == null ? <span className="text-gray-300">?</span> : v ? <span className="text-emerald-600">✓</span> : <span className="text-red-500">✗</span>);
  const r = (v: number | null | undefined) =>
    v == null ? <span className="text-gray-300">—</span> : <span className={v >= 0 ? "text-emerald-600" : "text-red-600"}>{pct(v)}</span>;
  return (
    <div className="bg-white rounded-xl shadow overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-gray-500 border-b">
            <th className="p-2">通過</th><th className="p-2">Ticker</th><th className="p-2">fwd PER</th><th className="p-2">EV/EBIT</th>
            <th className="p-2">SY</th><th className="p-2">EPS成長</th><th className="p-2">ROA</th>
            <th className="p-2 text-center">G1</th><th className="p-2 text-center">G2</th><th className="p-2 text-center">G3</th>
            <th className="p-2 text-center">G4</th><th className="p-2 text-center">G5</th><th className="p-2 text-center">G6</th>
            <th className="p-2">+1y</th><th className="p-2">+2y</th><th className="p-2">+3y</th><th className="p-2">月次パス</th>
          </tr>
        </thead>
        <tbody>
          {screens.map((s) => {
            const j = judge(s, toggles);
            return (
              <tr key={s.ticker} className={`border-b ${j.pass ? "bg-emerald-50" : ""}`}>
                <td className="p-2">{j.pass ? "●" : ""}</td>
                <td className="p-2 font-mono font-semibold">{s.ticker}{s.note && <span title={s.note} className="text-amber-500 ml-1">⚠</span>}</td>
                <td className="p-2">{s.fwd_per ?? "—"}</td>
                <td className="p-2">{s.ev_ebit ?? "—"}</td>
                <td className="p-2">{pct(s.sh_yield)}</td>
                <td className="p-2">{pct(s.eps_growth)}</td>
                <td className="p-2">{pct(s.roa)}</td>
                <td className="p-2 text-center">{g(j.gates.g1)}</td>
                <td className="p-2 text-center">{g(j.gates.g2)}</td>
                <td className="p-2 text-center">{g(j.gates.g3)}</td>
                <td className="p-2 text-center">{g(j.gates.g4)}</td>
                <td className="p-2 text-center">{g(j.gates.g5)}</td>
                <td className="p-2 text-center">{g(j.gates.g6)}</td>
                <td className="p-2">{r(hzMap[s.ticker]?.["1y"])}</td>
                <td className="p-2">{r(hzMap[s.ticker]?.["2y"])}</td>
                <td className="p-2">{r(hzMap[s.ticker]?.["3y"])}</td>
                <td className="p-2"><Spark points={sparkMap[s.ticker] ?? []} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RequestFullCalc({ toggles }: { toggles: Toggles }) {
  const [msg, setMsg] = useState("");
  return (
    <div className="bg-white rounded-xl shadow p-4 flex items-center gap-3 text-sm">
      <div className="text-gray-600">グリッド外の条件（売りルールA/B等の経路依存シミュ）が必要な場合：</div>
      <button className="bg-blue-600 text-white rounded px-3 py-1.5"
        onClick={async () => {
          const { error } = await supabase.from("rf_bt_run_requests").insert({ conditions: toggles, note: "app request" });
          setMsg(error ? `失敗: ${error.message}` : "リクエスト登録済み。日次ジョブ(rf-bt-request-runner)が処理し翌日反映されます。");
        }}>
        フル計算をリクエスト
      </button>
      {msg && <span className="text-gray-500">{msg}</span>}
    </div>
  );
}

function Audit({ screens }: { screens: Screen[] }) {
  return (
    <div className="space-y-4">
      <div className="bg-white rounded-xl shadow p-4 text-sm text-gray-600">
        すべての数字は一次ソースへ遡れます。ファンダ＝SEC EDGAR accession（下記リンク）、価格＝IBKR月次バー（Polygon分割係数で未調整化）。
        集計値の計算式・近似は各行の sources(JSON) に保存。
      </div>
      {screens.map((s) => (
        <div key={s.ticker} className="bg-white rounded-xl shadow p-4 text-sm">
          <div className="font-mono font-bold mb-1">{s.ticker} <span className="font-sans font-normal text-gray-400">screen {s.screen_date}</span></div>
          {s.sources && (
            <div className="space-y-1 text-gray-700">
              {"fy2015_accession" in s.sources && (
                <div>FY2015 10-K: <a className="text-blue-600 underline" target="_blank"
                  href={`https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=&type=10-K&dateb=&owner=include&count=10&search_text=${s.sources["fy2015_accession"]}`}>
                  accession {s.sources["fy2015_accession"]}</a></div>
              )}
              {"fy2014_accession" in s.sources && <div>FY2014 10-K: accession {s.sources["fy2014_accession"]}</div>}
              {"price_source" in s.sources && <div>価格: {s.sources["price_source"]}</div>}
              {"approx" in s.sources && <div className="text-amber-700">近似: {s.sources["approx"]}</div>}
            </div>
          )}
          {s.note && <div className="text-amber-600 mt-1">⚠ {s.note}</div>}
        </div>
      ))}
    </div>
  );
}
