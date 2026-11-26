import { FormEvent, useEffect, useState } from "react";
import { Cable, Database, Download, Leaf, LayoutDashboard, LogOut, RefreshCw, ScrollText, UserPlus, Waves, Zap } from "lucide-react";
import { App } from "./App";
import { useStatus } from "./useStatus";

type Me = { dashboards:{id:string;name:string;is_owner:boolean}[]; selection_required:boolean; user: { email:string; name:string; admin:boolean }; dashboard: { is_owner:boolean; id:string; setup_complete:boolean; system_start_at:string | null; has_projections?:boolean } };
type RateDirection = "import" | "export";
type RatePlan = { start_date:string; end_date:string; kind:"dynamic" | "flat"; pence:string };
type SavedSetup = { settings: { display_name:string; system_start_at:string | null; earnings_config?: { import_rates?: RatePlan[]; export_rates?: RatePlan[] } }; projections: { fiscal_year:number; generation_kwh:number; solar_gbp:number; battery_gbp:number; export_gbp:number; cost_gbp?:number | null; net_position_gbp?:number | null }[]; credentials?: { octopus?: Record<string,string>; ecoflow_developer?: Record<string,string>; ecoflow_app?: Record<string,string>; axle_vpp?: Record<string,string> } };
type ProjectionPlan = { year:string; generation_kwh:string; solar_gbp:string; battery_gbp:string; export_gbp:string; cost_gbp:string; net_position_gbp:string };
const csrf = () => document.cookie.split("; ").find((entry) => entry.startsWith("energy_csrf="))?.split("=")[1] ?? "";

function Login() {
  const signIn = new URLSearchParams(window.location.search).get("sign_in");
  const message = signIn === "not-invited" ? "This Google account has not been invited yet. Ask the dashboard owner to send an invitation to this email address." : signIn === "expired" ? "Your sign-in session expired. Please try again." : signIn === "failed" ? "We could not complete your Google sign-in. Please try again." : null;
  return <main className="access-page"><section className="access-card"><h1>Home Energy</h1><p>Sign in to view or set up your private energy dashboard.</p>{message && <p className="access-message" role="alert">{message}</p>}<a className="google-button" href="/api/auth/google"><img src="/google-signin/sign-in-with-google.png" alt="Sign in with Google" width="180" height="40" /></a><small>Access is by invitation only.</small></section></main>;
}

type Invitation = { email:string; type:string; expires_at:string; status:string; email_status:string; can_reinvite:boolean };

function InvitationPanel({ invitationType = "dashboard_access" }: { invitationType?:string }) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [invites, setInvites] = useState<Invitation[]>([]);
  async function loadInvites() {
    try {
      const response = await fetch(`/api/admin/invitations?invitation_type=${invitationType}`, {cache:"no-store"});
      if (!response.ok) throw new Error("Could not load invited users. Please try again.");
      setInvites((await response.json()).invitations);
    } catch (error) { setError(error instanceof Error ? error.message : "Could not load invited users."); }
    finally { setLoading(false); }
  }
  useEffect(() => { void loadInvites(); }, []);
  async function changeInvitation(recipient:string, type:string, remove = false) {
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await fetch("/api/admin/invitations", {
        method:remove ? "DELETE" : "POST",
        headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf()},
        body:JSON.stringify({email:recipient, type}),
      });
      if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail ?? "Invitation could not be updated.");
      setMessage(remove ? type === "dashboard_access" ? `Invitation deleted and dashboard access removed for ${recipient}.` : `Invitation and any dashboard owned by ${recipient} deleted, including its data.` : `Invitation email sent to ${recipient}.`);
      if (!remove) setEmail("");
      await loadInvites();
    } catch (error) { setError(error instanceof Error ? error.message : "Unable to reach the server. Please try again."); }
    finally { setBusy(false); }
  }
  function invite(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const recipient = email.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient) || recipient.length > 254) {
      setError("Enter a valid email address."); return;
    }
    void changeInvitation(recipient, invitationType);
  }
  return <section className="invitation-panel" aria-label="Invitations">
    <form onSubmit={invite} className="invitation-form">
      <label>Email address<input type="email" value={email} onChange={event => setEmail(event.target.value)} required maxLength={254} autoComplete="email" placeholder="name@example.com" disabled={busy} /></label>
      <button disabled={busy}>{busy ? "Updating…" : "Send invitation email"}</button>
    </form>
    {message && <p className="invite-success" role="status">{message}</p>}
    {error && <p className="setup-error" role="alert">{error}</p>}
    <h3>Invited users</h3>
    {loading ? <p role="status">Loading invited users…</p> : <div className="invitation-table-wrap"><table className="invitation-table">
      <thead><tr><th>Email</th><th>Email status</th><th>Invite status</th><th>Reinvite</th><th>Delete</th></tr></thead>
      <tbody>{invites.length === 0 ? <tr><td colSpan={5}>No users invited yet.</td></tr> : invites.map(item => <tr key={item.email}>
        <td>{item.email}</td><td>{item.email_status}</td><td>{item.status}</td>
        <td><button type="button" className="reinvite-link" disabled={busy || !item.can_reinvite} title={item.status === "Accepted" ? "Invitation already accepted" : `Available from ${new Date(item.expires_at).toLocaleString()}`} onClick={() => void changeInvitation(item.email, item.type)}>Reinvite</button></td>
        <td><button type="button" className="delete-invitation" disabled={busy} aria-label={`Delete invitation for ${item.email}`} onClick={() => void changeInvitation(item.email, item.type, true)}>Delete</button></td>
      </tr>)}</tbody>
    </table></div>}
    <small>Reinvite becomes available after seven days without acceptance. {invitationType === "new_dashboard" ? "Deleting an invitation deletes the invited user's dashboard and all of its data." : "Deleting an invitation also removes the user's access to the configured dashboard."}</small>
  </section>;
}

function DataExportPanel() {
  return <section className="data-export-panel" aria-label="Data export">
    <h2>Export energy data</h2>
    <p>Download a CSV for tariff analysis. It includes EcoFlow hourly energy readings and settled Octopus electricity import and export intervals at their original resolution, usually 30 minutes.</p>
    <p>The file is in a tidy format: each row has the UTC interval, duration, provider, source, metric, kWh, quality, and completeness. This makes it suitable for ChatGPT or another tariff comparison tool.</p>
    <a className="data-export-download" href="/api/exports/energy.csv">Download energy data CSV</a>
    <small>Only settled Octopus electricity readings are included. EcoFlow rows retain their recorded quality and completeness; gas and live telemetry are excluded.</small>
  </section>;
}

function SettingsOperationalPanel({ tab }: { tab:"connections" | "imports" | "logs" }) {
  const { status } = useStatus();
  const [operations, setOperations] = useState<any>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    const refresh = () => fetch("/api/operations", {cache:"no-store"}).then(response => response.ok ? response.json() : null).then(value => setOperations(value)).catch(() => {});
    void refresh(); const timer = window.setInterval(refresh, 5_000); return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (tab !== "logs") return;
    const refresh = () => fetch("/api/collector-logs", {cache:"no-store"}).then(response => response.ok ? response.json() : null).then(value => setLogs(value?.lines ?? [])).catch(() => {});
    void refresh(); const timer = window.setInterval(refresh, 2_000); return () => window.clearInterval(timer);
  }, [tab]);
  const retry = async () => { setRetrying(true); try { await fetch("/api/operations/retry-partials", {method:"POST", headers:{"X-CSRF-Token":csrf()}}); } finally { setRetrying(false); } };
  if (tab === "connections") return <section className="settings-operations-panel"><h2><Cable size={20} />Connections</h2><p>Your dashboard’s data sources and workspace services.</p><div className="settings-connection-list">{([["EcoFlow", status?.providers.ecoflow], ["Octopus Energy", status?.providers.octopus], ["Octopus Mini", status?.providers.octopus_mini], ["Octopus Gas", status?.providers.octopus_gas]] as Array<[string, any]>).map(([label, provider]) => <article key={label}><strong>{label}</strong><span className={provider?.state === "healthy" ? "settings-status-good" : ""}>{provider?.state === "healthy" ? "Connected" : provider?.state === "partial" ? "Partial" : provider?.state === "syncing" || provider?.state === "backfilling" ? "Syncing" : "Not connected"}</span><small>{provider?.reason ?? "Waiting for provider status."}</small></article>)}<article><strong>Database</strong><span className={status?.database === "ready" ? "settings-status-good" : ""}>{status?.database === "ready" ? "Ready" : "Unavailable"}</span><small>PostgreSQL</small></article><article><strong>Collector</strong><span className={status?.collector.state === "healthy" ? "settings-status-good" : ""}>{status?.collector.state === "healthy" ? "Running" : status?.collector.state ?? "Unknown"}</span><small>Heartbeat, telemetry maintenance, and provider imports.</small></article></div></section>;
  if (tab === "imports") return <section className="settings-operations-panel"><div className="settings-operations-heading"><div><h2><Database size={20} />Import statistics</h2><p>Coverage, last run, and persisted rows per source.</p></div><button type="button" className="secondary-button" onClick={() => void retry()} disabled={retrying}>{retrying ? "Queuing retry…" : <><RefreshCw size={16} /> Retry partial imports now</>}</button></div><div className="settings-import-list">{operations?.imports?.map((row:any) => <article key={`${row.source}-${row.dataset ?? "live"}`}><strong>{row.source}</strong><span>{row.dataset ?? "—"}</span><span>{row.coverage_start ? `${new Date(row.coverage_start).toLocaleDateString("en-GB")} – ${new Date(row.coverage_end).toLocaleDateString("en-GB")}` : "No coverage yet"}<small>{row.complete_windows} complete · {row.partial_windows} partial · {row.failed_windows} failed</small></span><span>{row.status === "running" ? "Running" : row.status === "partial" ? "Partial" : row.status === "complete" ? "Complete" : "Waiting"}<small>{row.rows_received ?? 0} / {row.rows_written ?? 0} received / persisted</small></span></article>) ?? <p>Loading import statistics…</p>}</div><small>Octopus settlement is delayed by 1–2 days; older gaps remain partial until supplied by the provider.</small></section>;
  return <section className="settings-operations-panel"><h2><ScrollText size={20} />Collector logs</h2><p>Recent collector output for this dashboard.</p><pre className="settings-log-output">{logs.length ? logs.join("\n") : "Waiting for collector logs…"}</pre></section>;
}

function Invitations({ onBack }: { onBack:() => void }) {
  return <main className="access-page"><section className="access-card invitation-card"><button type="button" className="back-link" onClick={onBack}>← Dashboard</button><InvitationPanel invitationType="new_dashboard" /></section></main>;
}

function Setup({ me, onSaved, editing = false, onReturn, onLogout }: { me:Me; onSaved:()=>void; editing?:boolean; onReturn?:()=>void; onLogout?:()=>void }) {
  const [saving, setSaving] = useState(false); const [error, setError] = useState("");
  const [projectionMode, setProjectionMode] = useState<"json" | "manual">("json");
  const emptyRate = (): RatePlan => ({ start_date:"", end_date:"", kind:"dynamic", pence:"" });
  const [importRates, setImportRates] = useState<RatePlan[]>([emptyRate()]);
  const [exportRates, setExportRates] = useState<RatePlan[]>([emptyRate()]);
  const [rateEditor, setRateEditor] = useState<RateDirection | null>(null);
  const emptyProjection = (): ProjectionPlan => ({year:"1", generation_kwh:"", solar_gbp:"", battery_gbp:"", export_gbp:"", cost_gbp:"", net_position_gbp:""});
  const [manualProjections, setManualProjections] = useState<ProjectionPlan[]>([emptyProjection()]);
  const [projectionEditor, setProjectionEditor] = useState(false);
  const [settingsTab, setSettingsTab] = useState<"dashboard" | "octopus" | "ecoflow" | "axle" | "export" | "connections" | "imports" | "logs" | "invite">(() => new URLSearchParams(window.location.search).get("tab") as "connections" | "imports" | "logs" ?? "dashboard");
  const [settingsSubTab, setSettingsSubTab] = useState<"ecoflow" | "rates" | "projection">("ecoflow");
  const [savedSetup, setSavedSetup] = useState<SavedSetup | null>(null);
  const [formKey, setFormKey] = useState(0);
  useEffect(() => {
    if (!editing) return;
    fetch("/api/setup", {cache:"no-store"}).then((response) => response.ok ? response.json() : null).then((saved: SavedSetup | null) => {
      if (!saved) return;
      setSavedSetup(saved);
      const config = saved.settings.earnings_config;
      setImportRates(config?.import_rates?.length ? config.import_rates : [emptyRate()]);
      setExportRates(config?.export_rates?.length ? config.export_rates : [emptyRate()]);
      setManualProjections(saved.projections.length ? saved.projections.map((item) => ({year:String(item.fiscal_year - new Date(saved.settings.system_start_at ?? Date.now()).getFullYear() + 1), generation_kwh:String(item.generation_kwh), solar_gbp:String(item.solar_gbp), battery_gbp:String(item.battery_gbp), export_gbp:String(item.export_gbp), cost_gbp:item.cost_gbp == null ? "" : String(item.cost_gbp), net_position_gbp:item.net_position_gbp == null ? "" : String(item.net_position_gbp)})) : [emptyProjection()]);
      setFormKey((key) => key + 1);
    }).catch(() => {});
  }, [editing]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setSaving(true); setError(""); const form = new FormData(event.currentTarget);
    let projection: unknown[] = [];
    try { projection = projectionMode === "json" ? JSON.parse(String(form.get("projections_json") || "[]")) : manualProjections.filter((item) => Object.values(item).some(Boolean)); if (!Array.isArray(projection)) throw new Error(); projection = projection.map((item) => typeof item === "object" && item !== null && "year" in item && !("fiscal_year" in item) ? {...item, fiscal_year:fiscalYear + Number((item as {year:unknown}).year) - 1} : item); }
    catch { setSaving(false); setError("Projection JSON must be an array of yearly targets."); return; }
    const payload = { display_name:form.get("display_name"), system_start_at:form.get("system_start_at"), earnings_config:{ import_rates:importRates, export_rates:exportRates }, projections:projection,
      credentials:{ octopus:{ api_key:form.get("octopus_api_key"), account_number:form.get("octopus_account_number"), mini_device_id:form.get("octopus_mini_device_id") }, ecoflow_developer:{ access_key:form.get("ecoflow_access_key"), secret_key:form.get("ecoflow_secret_key"), serial:form.get("ecoflow_serial"), battery_serials:form.get("ecoflow_battery_serials") }, ecoflow_app:{ email:form.get("ecoflow_email"), password:form.get("ecoflow_password") }, axle_vpp:{ api_key:form.get("axle_api_key") } } };
    const response = await fetch("/api/setup", {method:"PUT",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf()},body:JSON.stringify(payload)}); setSaving(false);
    if (!response.ok) { setError((await response.json().catch(()=>({}))).detail ?? "Setup could not be saved"); return; } onSaved();
  }
  const fiscalYear = new Date(me.dashboard.system_start_at ?? Date.now()).getFullYear();
  const rateSummary = (rates: RatePlan[]) => {
    const configured = rates.filter((rate) => rate.start_date || rate.end_date || rate.pence);
    if (!configured.length) return "Not set";
    return configured.some((rate) => rate.kind === "dynamic") ? "Dynamic rates" : "Flat rate";
  };
  const selectedRates = rateEditor === "import" ? importRates : exportRates;
  const setSelectedRates = rateEditor === "import" ? setImportRates : setExportRates;
  const selectedRateTitle = rateEditor === "import" ? "Consumption rate" : "Feed-in rate";
  const projectionDefault = savedSetup?.projections.length ? JSON.stringify(savedSetup.projections.map((item) => ({ year:item.fiscal_year - fiscalYear + 1, generation_kwh:item.generation_kwh, solar_gbp:item.solar_gbp, battery_gbp:item.battery_gbp, export_gbp:item.export_gbp, cost_gbp:item.cost_gbp, net_position_gbp:item.net_position_gbp })), null, 2) : JSON.stringify([{ year:1, generation_kwh:5305, solar_gbp:292.17, battery_gbp:233.74, export_gbp:452.31, total_benefit_gbp:978.22, cost_gbp:15076, net_position_gbp:-14097.78 }, { year:2, generation_kwh:5287, solar_gbp:312.74, battery_gbp:250.19, export_gbp:449.61, total_benefit_gbp:1012.54, cost_gbp:0, net_position_gbp:-13085.24 }], null, 2);
  const today = new Date().toLocaleDateString("en-CA");
  const rateIsInUse = (rate: RatePlan) => Boolean(rate.start_date && rate.end_date) && rate.start_date <= today && rate.end_date >= today;
  const rateIsExpired = (rate: RatePlan) => Boolean(rate.end_date) && rate.end_date < today;
  return <main className="access-page"><section className={`setup-card ${editing ? `settings-tab-${settingsTab} settings-subtab-${settingsSubTab}` : ""}`}>{!me.dashboard.setup_complete && onLogout && <button type="button" className="back-link cancel-setup" onClick={onLogout} disabled={saving}>Cancel setup</button>}{me.dashboard.setup_complete && onLogout && <button type="button" className="logout-button" aria-label="Log out" title="Log out" onClick={onLogout}><LogOut size={18} /></button>}{editing && onReturn && <button type="button" className="back-link" onClick={onReturn}>← Return to dashboard</button>}<h1>{editing ? "Dashboard settings" : "Set up your dashboard"}</h1><p>Provider credentials are encrypted before storage and are available only to your signed-in dashboard account.</p>{editing && <><nav className="settings-tabs" aria-label="Dashboard settings"><button type="button" className={settingsTab === "dashboard" ? "active" : ""} onClick={() => setSettingsTab("dashboard")}><LayoutDashboard size={16} />Dashboard name</button><button type="button" className={settingsTab === "octopus" ? "active" : ""} onClick={() => setSettingsTab("octopus")}><Waves size={16} />Octopus details</button><button type="button" className={settingsTab === "ecoflow" ? "active" : ""} onClick={() => setSettingsTab("ecoflow")}><Leaf size={16} />EcoFlow details</button><button type="button" className={settingsTab === "axle" ? "active" : ""} onClick={() => setSettingsTab("axle")}><Zap size={16} />Axle VPP</button>{me.dashboard.setup_complete && <button type="button" className={settingsTab === "export" ? "active" : ""} onClick={() => setSettingsTab("export")}><Download size={16} />Data export</button>}{me.dashboard.setup_complete && <button type="button" className={settingsTab === "connections" ? "active" : ""} onClick={() => setSettingsTab("connections")}><Cable size={16} />Connections</button>}{me.dashboard.setup_complete && <button type="button" className={settingsTab === "imports" ? "active" : ""} onClick={() => setSettingsTab("imports")}><Database size={16} />Imports</button>}{me.dashboard.setup_complete && <button type="button" className={settingsTab === "logs" ? "active" : ""} onClick={() => setSettingsTab("logs")}><ScrollText size={16} />Logs</button>}{me.dashboard.setup_complete && me.user.admin && <button type="button" className={settingsTab === "invite" ? "active" : ""} onClick={() => setSettingsTab("invite")}><UserPlus size={16} />Invite</button>}</nav>{settingsTab === "ecoflow" && <nav className="settings-subtabs" aria-label="EcoFlow settings"><button type="button" className={settingsSubTab === "ecoflow" ? "active" : ""} onClick={() => setSettingsSubTab("ecoflow")}>EcoFlow</button><button type="button" className={settingsSubTab === "rates" ? "active" : ""} onClick={() => setSettingsSubTab("rates")}>Electricity rate</button><button type="button" className={settingsSubTab === "projection" ? "active" : ""} onClick={() => setSettingsSubTab("projection")}>Annual projection</button></nav>}</>}{editing && me.dashboard.setup_complete && settingsTab === "export" && <DataExportPanel />}{editing && me.dashboard.setup_complete && (settingsTab === "connections" || settingsTab === "imports" || settingsTab === "logs") && <SettingsOperationalPanel tab={settingsTab} />}{editing && me.dashboard.setup_complete && settingsTab === "invite" && me.user.admin && <InvitationPanel />}<form key={formKey} className="setup-fields" hidden={editing && (["invite", "export", "connections", "imports", "logs"] as string[]).includes(settingsTab)} onSubmit={submit}>{error && <p className="setup-error">{error}</p>}
    <label className="dashboard-name-input"><input aria-label="Dashboard name" name="display_name" defaultValue={savedSetup?.settings.display_name ?? `${me.user.name}'s energy`} required /></label><label className="ecoflow-settings-field">System start date<input name="system_start_at" type="datetime-local" defaultValue={(savedSetup?.settings.system_start_at ?? me.dashboard.system_start_at)?.slice(0,16)} required /></label>
    <fieldset><legend>Octopus</legend><label>Account number<input name="octopus_account_number" defaultValue={savedSetup?.credentials?.octopus?.account_number} required={!editing} /></label><label>API key<input name="octopus_api_key" defaultValue={savedSetup?.credentials?.octopus?.api_key} required={!editing} /></label><label><span>Mini device ID <small>(optional)</small></span><input name="octopus_mini_device_id" defaultValue={savedSetup?.credentials?.octopus?.mini_device_id} /></label></fieldset>
    <fieldset className="ecoflow-settings-field"><legend>EcoFlow app</legend><label>Email<input name="ecoflow_email" type="email" defaultValue={savedSetup?.credentials?.ecoflow_app?.email} required={!editing} /></label><label>Password<input name="ecoflow_password" type="password" defaultValue={savedSetup?.credentials?.ecoflow_app?.password} required={!editing} /></label></fieldset>
    <fieldset className="ecoflow-settings-field"><legend>EcoFlow Developer API</legend><label>Access key<input name="ecoflow_access_key" defaultValue={savedSetup?.credentials?.ecoflow_developer?.access_key} required={!editing} /></label><label>Secret key<input name="ecoflow_secret_key" defaultValue={savedSetup?.credentials?.ecoflow_developer?.secret_key} required={!editing} /></label><label>PowerOcean serial<input name="ecoflow_serial" defaultValue={savedSetup?.credentials?.ecoflow_developer?.serial} required={!editing} /></label><label><span>Battery serial <small>(optional, comma separated)</small></span><input name="ecoflow_battery_serials" defaultValue={savedSetup?.credentials?.ecoflow_developer?.battery_serials} /></label></fieldset>
    <fieldset className="axle-settings-field"><legend>Axle VPP</legend><p>Use the API token from Axle Account → Integrations / API. It is encrypted before storage.</p><label>API key<input name="axle_api_key" type="password" autoComplete="off" defaultValue={savedSetup?.credentials?.axle_vpp?.api_key} /></label></fieldset><fieldset className="electricity-settings rates-settings-field"><legend>Electricity rates</legend><p>Set the import and export rate histories used to calculate earnings.</p><div className="rate-summary-list"><button type="button" className="rate-summary" onClick={() => setRateEditor("import")}><span><strong>Consumption rate</strong><small>Electricity imported from the grid</small></span><b>{rateSummary(importRates)}</b><i aria-hidden="true">›</i></button><button type="button" className="rate-summary" onClick={() => setRateEditor("export")}><span><strong>Feed-in rate</strong><small>Electricity exported to the grid</small></span><b>{rateSummary(exportRates)}</b><i aria-hidden="true">›</i></button></div></fieldset>
    <fieldset className="projection-settings-field"><legend>Annual projection <small>Optional</small></legend><div className="setup-mode"><button type="button" className={projectionMode === "json" ? "selected" : "secondary-button"} onClick={() => setProjectionMode("json")}>JSON input</button><button type="button" className={projectionMode === "manual" ? "selected" : "secondary-button"} onClick={() => setProjectionMode("manual")}>Enter year by year</button></div>{projectionMode === "json" ? <label>Projection targets JSON <small>Use year 1 for the first system year. The document's cost and net_position values are retained and displayed as the projected system position.</small><textarea name="projections_json" rows={10} defaultValue={projectionDefault} /></label> : <div className="projection-summary"><p>{manualProjections.filter((item) => item.generation_kwh).length ? `${manualProjections.filter((item) => item.generation_kwh).length === 1 ? "1 annual target configured" : `${manualProjections.filter((item) => item.generation_kwh).length} annual targets configured`}` : "No annual targets configured"}</p><button type="button" className="secondary-button" onClick={() => setProjectionEditor(true)}>Edit annual targets</button></div>}</fieldset>
    <button disabled={saving}>{saving ? "Saving setup…" : "Save dashboard setup"}</button></form></section>{rateEditor && <div className="rate-dialog-backdrop" role="presentation" onMouseDown={() => setRateEditor(null)}><section className="rate-dialog" role="dialog" aria-modal="true" aria-labelledby="rate-dialog-title" onMouseDown={(event) => event.stopPropagation()}><div className="rate-dialog-heading"><div><h2 id="rate-dialog-title">{selectedRateTitle}</h2><p>These rates value EcoFlow earnings. Dynamic rates use Octopus tariffs. Enter flat rates in pence/kWh: 12 means £0.12/kWh. Start and end dates are inclusive.</p></div><button type="button" className="dialog-close" aria-label="Close" onClick={() => setRateEditor(null)}>×</button></div><div className="rate-dialog-actions"><button type="button" className="secondary-button" onClick={() => setSelectedRates([emptyRate(), ...selectedRates])}>Add rate</button><button type="button" onClick={() => setRateEditor(null)}>Done</button></div><div className="rate-editor-list">{selectedRates.map((rate,index) => <section className="rate-editor-card" key={index}><div><strong>{rate.kind === "dynamic" ? "Dynamic rate" : "Flat rate"}</strong><span>{rateIsInUse(rate) && <b className="rate-in-use">In use</b>}{rateIsExpired(rate) && <b className="rate-expired">Expired</b>}</span></div><label>Start date<input type="date" value={rate.start_date} onChange={event => setSelectedRates(selectedRates.map((row, i) => i === index ? {...row, start_date:event.target.value} : row))} required /></label><label>End date<input type="date" value={rate.end_date} onChange={event => setSelectedRates(selectedRates.map((row, i) => i === index ? {...row, end_date:event.target.value} : row))} required /></label><label>Rate type<select value={rate.kind} onChange={event => setSelectedRates(selectedRates.map((row, i) => i === index ? {...row, kind:event.target.value as RatePlan["kind"]} : row))}><option value="dynamic">Dynamic · Octopus</option><option value="flat">Flat rate</option></select></label>{rate.kind === "flat" && <label>Rate (p/kWh)<input type="number" min="0" step="0.001" inputMode="decimal" value={rate.pence} onChange={event => setSelectedRates(selectedRates.map((row, i) => i === index ? {...row, pence:event.target.value.replace(/[^0-9.]/g, "")} : row))} required /></label>}<button type="button" className="remove-rate" disabled={selectedRates.length === 1} onClick={() => setSelectedRates(selectedRates.filter((_, i) => i !== index))}>Remove rate</button></section>)}</div></section></div>}{projectionEditor && <div className="rate-dialog-backdrop" role="presentation" onMouseDown={() => setProjectionEditor(false)}><section className="rate-dialog projection-dialog" role="dialog" aria-modal="true" aria-labelledby="projection-dialog-title" onMouseDown={(event) => event.stopPropagation()}><div className="rate-dialog-heading"><div><h2 id="projection-dialog-title">Annual targets</h2><p>Add one target for each installation year supplied by your installer.</p></div><button type="button" className="dialog-close" aria-label="Close" onClick={() => setProjectionEditor(false)}>×</button></div><div className="rate-dialog-actions"><button type="button" className="secondary-button" onClick={() => setManualProjections([{...emptyProjection(), year:String(manualProjections.length + 1)}, ...manualProjections])}>Add year</button><button type="button" onClick={() => setProjectionEditor(false)}>Done</button></div><div className="projection-editor-list">{manualProjections.map((item,index) => <section className="projection-editor-card" key={index}><div><strong>Year {item.year || index + 1}</strong></div><label>Year<input type="number" min="1" value={item.year} onChange={event => setManualProjections(manualProjections.map((row,i) => i === index ? {...row, year:event.target.value} : row))} /></label><label>Generation (kWh)<input type="number" min="0" step="0.001" value={item.generation_kwh} onChange={event => setManualProjections(manualProjections.map((row,i) => i === index ? {...row, generation_kwh:event.target.value} : row))} /></label><label>Solar savings (£)<input type="number" min="0" step="0.01" value={item.solar_gbp} onChange={event => setManualProjections(manualProjections.map((row,i) => i === index ? {...row, solar_gbp:event.target.value} : row))} /></label><label>Battery savings (£)<input type="number" min="0" step="0.01" value={item.battery_gbp} onChange={event => setManualProjections(manualProjections.map((row,i) => i === index ? {...row, battery_gbp:event.target.value} : row))} /></label><label>Export credit (£)<input type="number" min="0" step="0.01" value={item.export_gbp} onChange={event => setManualProjections(manualProjections.map((row,i) => i === index ? {...row, export_gbp:event.target.value} : row))} /></label><button type="button" className="remove-rate" disabled={manualProjections.length === 1} onClick={() => setManualProjections(manualProjections.filter((_,i) => i !== index))}>Remove year</button></section>)}</div></section></div>}</main>;
}

export function Access() {
  // Render the sign-in screen immediately. This avoids an inaccessible blank
  // page if a browser extension, proxy, or a Vite reload delays the session
  // probe; a valid session replaces it as soon as the probe completes.
  const [me,setMe] = useState<Me | null>(null);
  const [editing, setEditing] = useState(false);
  const [choosingDashboard, setChoosingDashboard] = useState(false);
  const [selectionError, setSelectionError] = useState("");
  const [selecting, setSelecting] = useState(false);
  const [path, setPath] = useState(() => window.location.pathname);
  const navigate = (nextPath: string) => { window.history.pushState({}, "", nextPath); setPath(nextPath); };
  const logout = async () => {
    await fetch("/api/auth/logout", {method:"POST", headers:{"X-CSRF-Token":csrf()}}).catch(() => {});
    setMe(null); navigate("/");
  };
  const load = async () => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/api/auth/me", { signal: controller.signal });
      setMe(response.ok ? await response.json() : null);
    } catch {
      // Authentication must fail open to the login page. A proxy restart or
      // offline API should never leave the browser on an indefinite spinner.
      setMe(null);
    } finally {
      window.clearTimeout(timeout);
    }
  };
  useEffect(() => { void load(); const onPopState = () => setPath(window.location.pathname); window.addEventListener("popstate", onPopState); return () => window.removeEventListener("popstate", onPopState); }, []);
  async function selectDashboard(id:string) {
    setSelecting(true); setSelectionError("");
    try {
      const response = await fetch("/api/auth/dashboard", {method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":csrf()}, body:JSON.stringify({dashboard_id:id})});
      if (!response.ok) throw new Error("Could not select this dashboard. Please refresh and try again.");
      setEditing(false); setChoosingDashboard(false); navigate("/"); await load();
    } catch (error) { setSelectionError(error instanceof Error ? error.message : "Could not select dashboard."); }
    finally { setSelecting(false); }
  }
  if (!me) return <Login />;
  if (me.selection_required || choosingDashboard) return <main className="access-page"><section className="access-card invitation-card"><button type="button" className="back-link dashboard-selector-logout" onClick={logout}><LogOut size={18} aria-hidden="true" />Log out</button><h1>Choose a dashboard</h1><p>Select the dashboard you want to view.</p><div className="dashboard-choice-list">{me.dashboards.map(item => <button type="button" key={item.id} disabled={selecting} onClick={() => void selectDashboard(item.id)}><strong>{item.name}</strong><span>{item.is_owner ? "Owner" : "Viewer"}</span></button>)}</div>{selectionError && <p role="alert" className="setup-error">{selectionError}</p>}</section></main>;
  if (path === "/create-dashboard-invite") return me.user.admin ? <Invitations onBack={() => navigate("/")} /> : <main className="access-page"><section className="access-card"><h1>Access denied</h1><p>Only the configured initial administrator can invite users.</p><button type="button" onClick={() => navigate("/")}>Return to dashboard</button></section></main>;
  if (!me.dashboard.is_owner && (path === "/settings" || editing || !me.dashboard.setup_complete)) return <main className="access-page"><section className="access-card"><h1>{me.dashboard.setup_complete ? "Access denied" : "Dashboard setup pending"}</h1><p>Only the dashboard owner can access dashboard settings.</p>{me.dashboard.setup_complete && <button type="button" onClick={() => { setEditing(false); navigate("/"); }}>Return to dashboard</button>}<button type="button" onClick={logout}>Log out</button></section></main>;
  if (!me.dashboard.setup_complete || path === "/settings" || editing) return <Setup me={me} editing={me.dashboard.setup_complete && (editing || path === "/settings")} onReturn={() => navigate("/")} onLogout={logout} onSaved={() => { setEditing(false); navigate("/"); load(); }} />;
  return <App key={me.dashboard.id} onLogout={!me.dashboard.is_owner ? logout : undefined} onSwitchDashboard={me.dashboards.length > 1 ? () => setChoosingDashboard(true) : undefined} onSettings={me.dashboard.is_owner ? () => navigate("/settings") : undefined} hasSavedProjections={Boolean(me.dashboard.has_projections)} />;
}
