import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, ArrowUpRight, Check, Copy, Gauge, Link2, MousePointer2, Plus, Radio, TriangleAlert, Zap } from "lucide-react";
import "./styles.css";
import { scoreLoadTest } from "./score";

const API = "/api";

function formatDate(value) {
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(value));
}

function App() {
  const [links, setLinks] = useState([]);
  const [stats, setStats] = useState({ links: 0, clicks: 0, daily: [] });
  const [url, setUrl] = useState("");
  const [alias, setAlias] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [testCode, setTestCode] = useState("");
  const [duration, setDuration] = useState(5);
  const [targetRps, setTargetRps] = useState(100);
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState(null);

  const load = async () => {
    try {
      const [linksResponse, statsResponse] = await Promise.all([fetch(`${API}/links`), fetch(`${API}/stats`)]);
      if (!linksResponse.ok || !statsResponse.ok) throw new Error("Service unavailable");
      const loadedLinks = await linksResponse.json();
      setLinks(loadedLinks);
      setTestCode((current) => current || loadedLinks[0]?.code || "");
      setStats(await statsResponse.json());
    } catch {
      setError("Cannot reach the short-link service. Check that Docker is running.");
    }
  };

  useEffect(() => {
    let stopped = false;
    let timer;
    async function refresh() {
      await load();
      if (!stopped) timer = window.setTimeout(refresh, 1000);
    }
    refresh();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, []);

  async function shorten(event) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      const response = await fetch(`${API}/links`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, alias: alias || null }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Could not shorten this URL");
      setUrl(""); setAlias("");
      await load();
      copy(data.short_url, data.code);
    } catch (reason) { setError(reason.message); }
    finally { setBusy(false); }
  }

  async function copy(value, code) {
    await navigator.clipboard.writeText(value);
    setCopied(code);
    window.setTimeout(() => setCopied(""), 1800);
  }

  async function runLoadTest(event) {
    event.preventDefault();
    setTesting(true); setResult(null); setError("");
    try {
      const response = await fetch(`${API}/load-test`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: testCode, duration_seconds: Number(duration), target_rps: Number(targetRps) }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Load test failed");
      setResult(data);
      await load();
    } catch (reason) { setError(reason.message); }
    finally { setTesting(false); }
  }

  const maxClicks = Math.max(1, ...stats.daily.map((item) => item.clicks));
  const score = result ? scoreLoadTest(result) : null;

  return <main>
    <nav aria-label="Primary">
      <a className="brand" href="#top"><span className="mark"><Zap size={17} /></span> SHORT/CIRCUIT</a>
      <span className="status"><i /> LOCAL SYSTEM ONLINE</span>
    </nav>

    <section className="hero" id="top">
      <div className="eyebrow"><Radio size={14} /> PERSONAL LINK INFRASTRUCTURE</div>
      <h1>Less URL.<br/><span>More signal.</span></h1>
      <p>Create compact links, track every visit, and keep the details where you can find them.</p>

      <form onSubmit={shorten}>
        <label htmlFor="url">DESTINATION URL</label>
        <div className="url-row">
          <Link2 aria-hidden="true" />
          <input id="url" type="url" required placeholder="https://example.com/your/very/long/link" value={url} onChange={(e) => setUrl(e.target.value)} />
          <button type="submit" disabled={busy}>{busy ? "ROUTING…" : "SHORTEN"}<ArrowUpRight size={18}/></button>
        </div>
        <div className="alias-row">
          <label htmlFor="alias">CUSTOM ALIAS <span>OPTIONAL</span></label>
          <div><span>/</span><input id="alias" minLength="3" maxLength="24" pattern="[A-Za-z0-9_-]+" placeholder="my-link" value={alias} onChange={(e) => setAlias(e.target.value)} /></div>
        </div>
      </form>
      <p className="message" role="status" aria-live="polite">{error}</p>
    </section>

    <section className="dashboard" aria-label="Link overview">
      <div className="metrics">
        <div><span>LINKS CREATED</span><strong>{String(stats.links).padStart(2, "0")}</strong></div>
        <div><span>TOTAL CLICKS</span><strong>{String(stats.clicks).padStart(2, "0")}</strong></div>
      </div>
      <div className="chart-panel">
        <div className="section-heading"><div><span>TRAFFIC PULSE</span><h2>Last 7 days</h2></div><MousePointer2 size={20}/></div>
        <div className="chart" aria-label="Daily clicks chart">
          {stats.daily.map((item) => <div className="bar-cell" key={item.day}>
            <span className="bar-value">{item.clicks}</span>
            <div className="bar" style={{ height: `${Math.max(5, (item.clicks / maxClicks) * 100)}%` }} />
            <time>{new Date(`${item.day}T00:00:00`).toLocaleDateString(undefined, { weekday: "short" })}</time>
          </div>)}
        </div>
      </div>
    </section>

    <section className="load-lab" aria-labelledby="load-title">
      <div className="section-heading"><div><span>LOAD LAB / ISOLATED CONTAINER</span><h2 id="load-title">Pressure test a route</h2></div><Gauge size={22}/></div>
      <div className="lab-grid">
        <form className="load-controls" onSubmit={runLoadTest}>
          <div className="field"><label htmlFor="test-link">SHORT LINK</label><select id="test-link" value={testCode} onChange={(e) => setTestCode(e.target.value)} disabled={!links.length}>{links.length ? links.map((link) => <option key={link.code} value={link.code}>/{link.code}</option>) : <option>No links available</option>}</select></div>
          <div className="field"><label htmlFor="duration">DURATION <span>{duration}s</span></label><input id="duration" type="range" min="1" max="15" value={duration} onChange={(e) => setDuration(e.target.value)} /></div>
          <div className="field"><label htmlFor="target-rps">TARGET RATE <span>{targetRps} rps</span></label><input id="target-rps" type="range" min="10" max="1000" step="10" value={targetRps} onChange={(e) => setTargetRps(e.target.value)} /></div>
          <button type="submit" disabled={testing || !links.length}><Activity size={17}/>{testing ? `TESTING ${duration}s…` : "RUN LOAD TEST"}</button>
          <p><TriangleAlert size={14}/> Traffic goes through nginx to FastAPI on the same path a browser uses, with browser headers, at a fixed arrival rate that does not slow down when the server does. The 307 is not followed, so the destination host is never contacted. Each redirect reads the link and commits one click row; load tests count in analytics.</p>
        </form>
        <div className={`load-results ${result ? "has-results" : ""}`} aria-live="polite">
          {result ? <>
            <div className="result-primary">
              <span>SUSTAINED THROUGHPUT</span>
              <strong>{result.rps.toLocaleString()}</strong>
              <small>OF {result.target_rps.toLocaleString()} RPS OFFERED</small>
              <p className={`verdict ${result.sustained_target ? "good" : "bad"}`}>
                {result.sustained_target
                  ? `Held ${result.target_rps} rps, no errors.`
                  : `Did not hold ${result.target_rps} rps. ${result.errors.toLocaleString()} of ${result.requests_planned.toLocaleString()} requests missed the window — ${result.late.toLocaleString()} answered late, ${result.abandoned.toLocaleString()} never answered.`}
              </p>
              {!result.generator.kept_schedule && <p className="verdict bad">
                Generator did not offer the full {result.target_rps} rps — it fell {result.generator.schedule_lag_ms_p95} ms behind its own schedule (p95){result.generator.cpu_bound ? `, using ${Math.round(result.generator.cpu_fraction * 100)}% of a core on each of ${result.generator.processes} processes` : ""}. Treat the rate above as a floor, not a measurement. Lower the target.
              </p>}
            </div>
            <div className="percentiles">{Object.entries(result.latency_ms).map(([key, value]) => <div key={key}><span>{key.toUpperCase()} LATENCY</span><strong>{value}<small>ms</small></strong></div>)}</div>
            <div className="load-note">
              <span>LATENCY BASIS</span>
              <p>From scheduled arrival, so queue wait counts. Server time alone: p50 {result.service_latency_ms.p50} ms · p95 {result.service_latency_ms.p95} ms · p99 {result.service_latency_ms.p99} ms. Path: {result.path}.</p>
            </div>
            <div className="error-meter"><span>ERROR RATE</span><strong className={result.error_rate ? "bad" : "good"}>{result.error_rate}%</strong><small>{result.errors} failed / {result.requests.toLocaleString()} total</small></div>
            <div className="test-score">
              <div className="score-heading"><div><span>PERFORMANCE SCORE</span><p>Local comparison rubric</p></div><strong>{score.total}<small> / 100</small></strong></div>
              <meter min="0" max="100" value={score.total} aria-label="Performance score out of 100" />
              <p>Throughput {score.throughput}/30 · Latency {score.latency}/40 · Reliability {score.reliability}/30</p>
              <details><summary>How the score works</summary>
                <p>Throughput earns full points when the full offered rate is sustained. Latency targets: p50 ≤ 50 ms, p95 ≤ 150 ms, p99 ≤ 300 ms, weighted 20%, 30%, and 50%. Above each target, points decrease proportionally; latency points also scale by successful-request percentage.</p>
                <p>Reliability earns 30 points at 0% errors and decreases linearly to 0 at 5% errors. Total is rounded before display; component rounding may differ by one point. Compare runs at the same target rate, duration, and machine conditions. These are app-defined targets, not a production readiness rating.</p>
              </details>
            </div>
          </> : <div className="result-empty"><Activity/><strong>READY TO MEASURE</strong><p>Run a controlled burst to see throughput, tail latency, and failures.</p></div>}
        </div>
      </div>
    </section>

    <section className="history">
      <div className="section-heading"><div><span>LINK ARCHIVE</span><h2>Recent routes</h2></div><span className="count">{links.length} TOTAL</span></div>
      {links.length === 0 ? <div className="empty"><Plus/><h3>Your archive is clear.</h3><p>Shorten a URL above. Its route and click count will appear here.</p></div> :
        <div className="link-list">{links.map((link) => <article key={link.id}>
          <div className="route"><a href={link.short_url} target="_blank" rel="noreferrer">/{link.code}<ArrowUpRight size={14}/></a><p title={link.target_url}>{link.target_url}</p></div>
          <div className="link-meta"><span><MousePointer2 size={14}/>{link.clicks} {link.clicks === 1 ? "click" : "clicks"}</span><time>{formatDate(link.created_at)}</time></div>
          <button className="copy" onClick={() => copy(link.short_url, link.code)} aria-label={`Copy short link ${link.code}`}>
            {copied === link.code ? <Check size={17}/> : <Copy size={17}/>} {copied === link.code ? "COPIED" : "COPY"}
          </button>
        </article>)}</div>}
    </section>
    <footer><span>SHORT/CIRCUIT</span><a href="/architecture.html" style={{ color: "var(--ink)" }}>SYSTEM MAP ↗</a><a href="/results.html" style={{ color: "var(--ink)" }}>CHANGE & RESULT LOG ↗</a><p>Built for links that need less room.</p></footer>
  </main>;
}

createRoot(document.getElementById("root")).render(<React.StrictMode><App /></React.StrictMode>);
