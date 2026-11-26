import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Battery,
  BatteryCharging,
  Cable,
  CalendarDays,
  CarFront,
  ChevronRight,
  CircleHelp,
  Coins,
  Database,
  Grid2X2,
  ArrowLeftRight,
  House,
  Leaf,
  Menu,
  Plus,
  LogOut,
  X,
  PlugZap,
  Radio,
  ReceiptPoundSterling,
  ShieldCheck,
  Settings,
  Scale,
  Sun,
  Waves,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import { useStatus } from "./useStatus";

const OCTOPUS_CHART_COLOR = "#806281";

function Metric({
  title,
  description,
  icon: Icon,
  tone,
  value = "—",
  connected = false,
  updating = false,
}: {
  title: string;
  description: string;
  icon: LucideIcon;
  tone: string;
  value?: string;
  connected?: boolean;
  updating?: boolean;
}) {
  return (
    <article className="metric card">
      <div className="metric-top">
        <span className={`icon-tile ${tone}`}>
          <Icon size={21} strokeWidth={1.7} />
        </span>
        <span className="metric-inline-status">{updating ? <span className="loading-spinner" aria-label="Refreshing live power" /> : <><span className={`empty-dot ${connected ? "live" : ""}`} />{connected ? "Live telemetry" : "Not connected"}</>}</span>
      </div>
      <h3>{title}</h3>
      <div className="metric-value">
        <span>{value}</span>
        <span className="unit">kW</span>
      </div>
      <Note className="metric-note">{description}</Note>
    </article>
  );
}


function EnergyChart({ rows, mode, identifiers = {} }: { rows: {at:string;source:string;metric:string;value:string}[]; mode:string; identifiers?: Record<string,string> }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const config: Record<string, {label:string; metrics:string[]; color:string}> = {
      Solar:{label:"Solar generation",metrics:["solar_generation_energy"],color:"#e5a900"},
      Home:{label:"Home consumption",metrics:["home_consumption_energy","ecoflow_to_home"],color:"#5e8ef7"},
      Grid:{label:"Grid exchange",metrics:["grid_import_energy","grid_export_energy"],color:"#45c8d9"},
      Battery:{label:"Battery charge & discharge",metrics:["battery_charge_energy","battery_discharge_energy"],color:"#2fcf7d"},
      Gas:{label:"Gas consumption",metrics:["gas_consumption_kwh"],color:"#66cde3"}
    };
    const active=config[mode];
    const filtered=rows.filter(r=>active.metrics.includes(r.metric));
    const timestamps=[...new Set(filtered.map(row=>row.at))].sort();
    const labels=timestamps.map(at=>new Date(at).toLocaleString("en-GB",{timeZone:"Europe/London",hour:"2-digit",minute:"2-digit",day:"2-digit",month:"short"}));
    const valueFor=(source:string, metric:string)=>timestamps.map(at=>filtered.filter(row=>row.at===at && row.source===source && row.metric===metric).reduce((sum,row)=>sum+Number(row.value),0));
    const axis={type:"category" as const,data:labels,axisLabel:{hideOverlap:true}};
    const yAxis={type:"value" as const,name:"kWh",splitLine:{lineStyle:{type:"dashed" as const}}};
    if (mode === "Grid") {
      const lineValues = (source:string, metric:string) => timestamps.map(at => {
        const matches = filtered.filter(row => row.at === at && row.source === source && row.metric === metric);
        return matches.length ? matches.reduce((sum,row) => sum + Number(row.value), 0) : null;
      });
      const line = (name:string, source:string, metric:string, xAxisIndex:number, yAxisIndex:number) => ({
        type:"line", name, xAxisIndex, yAxisIndex, data:lineValues(source, metric),
        smooth:true, showSymbol:false, connectNulls:false, lineStyle:{width:3}, areaStyle:{opacity:0.08}
      });
      chart.setOption({
        color:["#45c8d9", OCTOPUS_CHART_COLOR],
        tooltip:{trigger:"axis",valueFormatter:(value:number)=>value.toFixed(3)+" kWh"},
        legend:{top:0,data:["EcoFlow", "Octopus"]},
        title:[
          {text:`Import (${identifiers.ecoflow_octopus_import_mpan ?? "MPAN unavailable"})`,left:"center",top:26,textStyle:{fontSize:13,fontWeight:600}},
          {text:`Export (${identifiers.ecoflow_octopus_export_mpan ?? "MPAN unavailable"})`,left:"center",top:"53%",textStyle:{fontSize:13,fontWeight:600}}
        ],
        grid:[{left:48,right:20,top:58,height:"31%"},{left:48,right:20,top:"62%",bottom:42}],
        xAxis:[{...axis,gridIndex:0,axisLabel:{show:false}},{...axis,gridIndex:1}],
        yAxis:[{...yAxis,gridIndex:0},{...yAxis,gridIndex:1}],
        series:[
          line("EcoFlow", "ecoflow_powerocean", "grid_import_energy", 0, 0),
          line("Octopus", "octopus_import", "grid_import_energy", 0, 0),
          line("EcoFlow", "ecoflow_powerocean", "grid_export_energy", 1, 1),
          line("Octopus", "octopus_export", "grid_export_energy", 1, 1)
        ]
      });
    } else if (mode === "Battery") {
      chart.setOption({
        color:["#2fcf7d","#e5a900"],
        tooltip:{trigger:"axis",valueFormatter:(value:number)=>value.toFixed(3)+" kWh"},
        legend:{top:0,data:["Charge","Discharge"]},
        grid:{left:48,right:20,top:38,bottom:42},
        xAxis:axis,
        yAxis,
        series:[
          {type:"line",name:"Charge",data:valueFor("ecoflow_powerocean","battery_charge_energy"),smooth:true,showSymbol:false,lineStyle:{width:3},areaStyle:{opacity:0.08}},
          {type:"line",name:"Discharge",data:valueFor("ecoflow_powerocean","battery_discharge_energy"),smooth:true,showSymbol:false,lineStyle:{width:3},areaStyle:{opacity:0.08}}
        ]
      });
    } else {
      const grouped=new Map<string,number>();
      filtered.forEach(row=>grouped.set(row.at,(grouped.get(row.at)||0)+Number(row.value)));
      const points=[...grouped.entries()].sort((a,b)=>a[0].localeCompare(b[0]));
      chart.setOption({color:[active.color],tooltip:{trigger:"axis",valueFormatter:(value:number)=>value.toFixed(3)+" kWh"},grid:{left:48,right:20,top:28,bottom:42},xAxis:{type:"category",data:points.map(point=>new Date(point[0]).toLocaleString("en-GB",{timeZone:"Europe/London",hour:"2-digit",minute:"2-digit",day:"2-digit",month:"short"})),axisLabel:{hideOverlap:true}},yAxis,series:[{type:"bar",name:active.label,data:points.map(point=>point[1]),barMaxWidth:20,itemStyle:{borderRadius:[3,3,0,0]}}]});
    }
    const observer=new ResizeObserver(()=>chart.resize()); observer.observe(ref.current); return()=>{observer.disconnect();chart.dispose()};
  },[rows,mode]);
  return <div className={`energy-chart ${mode === "Grid" ? "grid-energy-chart" : ""}`} ref={ref} role="img" aria-label={mode === "Grid" ? "Grid import and export charts comparing EcoFlow and Octopus" : mode + " energy chart"} />;
}

function RateCard({ rate, mpan }: { rate: {code:string;label:string;unit_rates:{at:string;pence:string}[];standing_charge_pence:string | null;variable?:boolean}; mpan?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const values = rate.unit_rates.map((point) => Number(point.pence));
  const variable = rate.variable ?? new Set(values.map((value) => value.toFixed(6))).size > 1;
  useEffect(() => {
    if (!variable || !ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      color:[rate.code === "export" ? "#6d90f5" : rate.code === "gas" ? "#66cde3" : OCTOPUS_CHART_COLOR],
      tooltip:{trigger:"axis",valueFormatter:(value:number) => value.toFixed(3) + " p/kWh"},
      grid:{left:42,right:12,top:12,bottom:32},
      xAxis:{type:"category",data:rate.unit_rates.map((point) => new Date(point.at).toLocaleTimeString("en-GB", {timeZone:"Europe/London",hour:"2-digit",minute:"2-digit"})),axisLabel:{hideOverlap:true}},
      yAxis:{type:"value",name:"p/kWh",splitLine:{lineStyle:{type:"dashed"}}},
      series:[{type:"line",data:values,smooth:true,showSymbol:false,lineStyle:{width:3},areaStyle:{opacity:0.08}}],
    });
    const observer = new ResizeObserver(() => chart.resize()); observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [rate, variable]);
  return <article className={`rate-card rate-${rate.code}`}><h3>{mpan ? `${rate.label} (${mpan})` : rate.label}</h3>{rate.unit_rates.length === 0 ? <strong>—</strong> : variable ? <div ref={ref} className="rate-chart" role="img" aria-label={rate.label + " rate chart"} /> : <strong>{values[0].toFixed(3)} <small>p/kWh</small></strong>}<p>Standing charge <b>{rate.standing_charge_pence === null ? "—" : Number(rate.standing_charge_pence).toFixed(3) + " p/day"}</b></p></article>;
}

function RateLoadingCard({ code, label, mpan }: { code: string; label: string; mpan?: string }) {
  return <article className={`rate-card rate-${code} rate-loading-card`}><h3>{mpan ? `${label} (${mpan})` : label}</h3><span className="loading-spinner" aria-label={`Loading ${label} rate`} /><p>Standing charge <b>Loading</b></p></article>;
}

function Note({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <p className={`comparison-note ${className}`}><CircleHelp size={14} aria-hidden="true" /><span>{children}</span></p>;
}

function ConnectionRow({
  icon: Icon,
  title,
  detail,
  label,
  help,
  good = false,
}: {
  icon: LucideIcon;
  title: string;
  detail: string;
  label: string;
  help?: string;
  good?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="connection-row">
      <Icon size={19} strokeWidth={1.7} />
      <div className="connection-copy">
        <h3>{title}</h3>
        <p>{detail}</p>
      </div>
      {help ? <button type="button" className={`status-badge status-badge-button ${good ? "good" : ""}`} title="Show troubleshooting guidance" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{label}</button> : <span className={`status-badge ${good ? "good" : ""}`}>{label}</span>}
      {expanded && help && <p className="connection-help" role="status">{help}</p>}
    </div>
  );
}

type DashboardTab = "overview" | "tariff-comparison" | "earnings" | "net-position" | "bills" | "history" | "system" | "vpp" | "connections" | "imports" | "console" | "docker-logs";
type TariffCandidate = { name:string; import_tariff_code:string; export_tariff_code:string };
type TariffComparisonResult = TariffCandidate & { import_cost_gbp:string | null; export_credit_gbp:string | null; standing_charge_gbp:string | null; net_position_gbp:string | null; reason:string | null };
type TariffUsagePeriod = { key:string; label:string; hours:string; kwh:string; percent:string };
type TariffDaily = { date:string; import_kwh:string; export_kwh:string; candidates:Array<{import_cost_gbp:string | null; export_credit_gbp:string | null; standing_charge_gbp:string | null; net_position_gbp:string | null} | null> };
const csrf = () => document.cookie.split("; ").find((entry) => entry.startsWith("energy_csrf="))?.split("=")[1] ?? "";
const storedTariffBatteryDispatch = () => {
  try {
    const value = JSON.parse(localStorage.getItem("home-energy-tariff-battery-dispatch") ?? "{}");
    return { enabled: Boolean(value.enabled), discharge_percent: String(value.discharge_percent ?? "50"), start_time: String(value.start_time ?? "17:00"), end_time: String(value.end_time ?? "19:00") };
  } catch { return { enabled:false, discharge_percent:"50", start_time:"17:00", end_time:"19:00" }; }
};

export function App({ onSettings, onSwitchDashboard, onLogout, hasSavedProjections = false }: { onSettings?: () => void; onSwitchDashboard?: () => void; onLogout?: () => void; hasSavedProjections?: boolean }) {
  const { status, connection } = useStatus();
  useEffect(() => { document.getElementById("boot-loading")?.remove(); }, []);
  const [overview, setOverview] = useState<{energy: Record<string, string>; live: Record<string, {value: string}>; periods?: Record<string, string | null>; total_earnings_gbp?: string | null; energy_independence_percent?: string | null; projection?: {year:number;label:string;actual_kwh:string;target_kwh:string;forecast_kwh:string;progress_percent:string | null;on_track:boolean | null;target_benefit_gbp:string;actual_benefit_gbp:string | null;benefit_progress_percent:string | null;target_system_cost_gbp:string | null;target_system_net_position_gbp:string | null;actual_system_benefit_gbp:string | null;actual_system_net_position_gbp:string | null;system_position_progress_percent:string | null;target_savings:{solar_gbp:string;battery_gbp:string;export_gbp:string};actual_savings:{solar_gbp:string | null;battery_gbp:string | null;export_gbp:string | null}} | null; net_position?: Record<string, {electricity_only_gbp:string | null;including_gas_gbp:string | null;ecoflow_fallback?:boolean}>; identifiers?: Record<string,string>} | null>(null);
  const [todayOverview, setTodayOverview] = useState<{energy: Record<string, string>; live: Record<string, {value: string}>; periods?: Record<string, string | null>; total_earnings_gbp?: string | null; energy_independence_percent?: string | null; projection?: {year:number;label:string;actual_kwh:string;target_kwh:string;forecast_kwh:string;progress_percent:string | null;on_track:boolean | null;target_benefit_gbp:string;actual_benefit_gbp:string | null;benefit_progress_percent:string | null;target_system_cost_gbp:string | null;target_system_net_position_gbp:string | null;actual_system_benefit_gbp:string | null;actual_system_net_position_gbp:string | null;system_position_progress_percent:string | null;target_savings:{solar_gbp:string;battery_gbp:string;export_gbp:string};actual_savings:{solar_gbp:string | null;battery_gbp:string | null;export_gbp:string | null}} | null; net_position?: Record<string, {electricity_only_gbp:string | null;including_gas_gbp:string | null;ecoflow_fallback?:boolean}>; identifiers?: Record<string,string>} | null>(null);
  const todayDate = new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" });
  const [selectedDate, setSelectedDate] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" }));
  const [historyDate, setHistoryDate] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" }));
  const [earningsDate, setEarningsDate] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" }));
  const [netCalendarDate, setNetCalendarDate] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" }));
  const [billsDate, setBillsDate] = useState(() => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/London" }));
  const [period, setPeriod] = useState("day");
  const [dailySummaryPeriod, setDailySummaryPeriod] = useState<"month" | "year" | "lifetime">("month");
  const [netPositionPeriod, setNetPositionPeriod] = useState<"day" | "month" | "fiscal_year" | "lifetime">("day");
  const [activeTab, setActiveTab] = useState<DashboardTab>("overview");
  const [tariffComparisonStart, setTariffComparisonStart] = useState(() => { const value = new Date(); value.setDate(value.getDate() - 30); return value.toLocaleDateString("en-CA", {timeZone:"Europe/London"}); });
  const [tariffComparisonEnd, setTariffComparisonEnd] = useState(() => new Date().toLocaleDateString("en-CA", {timeZone:"Europe/London"}));
  const [tariffBatteryDispatchEnabled, setTariffBatteryDispatchEnabled] = useState(() => storedTariffBatteryDispatch().enabled);
  const [tariffBatteryCapacity, setTariffBatteryCapacity] = useState<string | null>(null);
  const [tariffBatteryDischargePercent, setTariffBatteryDischargePercent] = useState(() => storedTariffBatteryDispatch().discharge_percent);
  const [tariffBatteryDischargeStart, setTariffBatteryDischargeStart] = useState(() => storedTariffBatteryDispatch().start_time);
  const [tariffBatteryDischargeEnd, setTariffBatteryDischargeEnd] = useState(() => storedTariffBatteryDispatch().end_time);
  useEffect(() => {
    localStorage.setItem("home-energy-tariff-battery-dispatch", JSON.stringify({enabled:tariffBatteryDispatchEnabled, discharge_percent:tariffBatteryDischargePercent, start_time:tariffBatteryDischargeStart, end_time:tariffBatteryDischargeEnd}));
  }, [tariffBatteryDispatchEnabled, tariffBatteryDischargePercent, tariffBatteryDischargeStart, tariffBatteryDischargeEnd]);
  const [tariffCandidates, setTariffCandidates] = useState<TariffCandidate[]>([{name:"Current tariff", import_tariff_code:"", export_tariff_code:""}]);
  const [tariffCodeOptions, setTariffCodeOptions] = useState<{import_options:{code:string;label:string}[];export_options:{code:string;label:string}[]}>({import_options:[], export_options:[]});
  const [tariffComparisonResults, setTariffComparisonResults] = useState<TariffComparisonResult[]>([]);
  const [tariffComparisonSummary, setTariffComparisonSummary] = useState<{settled_import_kwh:string;settled_export_kwh:string;simulated_export_kwh:string} | null>(null);
  const [tariffComparisonInsights, setTariffComparisonInsights] = useState<TariffUsagePeriod[]>([]);
  const [tariffComparisonDaily, setTariffComparisonDaily] = useState<TariffDaily[]>([]);
  const [tariffComparisonLoading, setTariffComparisonLoading] = useState(false);
  const [tariffComparisonError, setTariffComparisonError] = useState("");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [calendarDetailDate, setCalendarDetailDate] = useState<string | null>(null);
  const [historyMode, setHistoryMode] = useState("Solar");
  const [historyRows, setHistoryRows] = useState<{at:string; source:string; metric:string; value:string}[]>([]);
  const [historyCosts, setHistoryCosts] = useState<Record<string, string | null>>({});
  const [vpp, setVpp] = useState<{configured:boolean; event:{start_at:string;end_at:string;direction:string;state:string;settled_export_kwh:string} | null; events:{start_at:string;end_at:string;direction:string;state:string;settled_export_kwh:string}[]} | null>(null);
  const [vppError, setVppError] = useState("");
  const [octopusRates, setOctopusRates] = useState<{code:string;label:string;unit_rates:{at:string;pence:string}[];standing_charge_pence:string | null}[]>([]);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [todayLoading, setTodayLoading] = useState(true);
  const [projectionLoading, setProjectionLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [ratesLoading, setRatesLoading] = useState(false);
  const [operations, setOperations] = useState<{compose_file?: string; imports: any[]; events: any[]; received: any[]; health: any[]; pending_persistence: number}>({imports: [], events: [], received: [], health: [], pending_persistence: 0});
  const [retryingPartials, setRetryingPartials] = useState(false);
  const [operationsLoading, setOperationsLoading] = useState(false);
  const [collectorLogs, setCollectorLogs] = useState<string[]>([]);
  const [calendarEarnings, setCalendarEarnings] = useState<{date:string; earnings_gbp:string | null; octopus_earnings_gbp:string | null; home_gbp?:string | null; settled_export_gbp?:string | null; octopus_incomplete?:boolean; has_override?:boolean}[]>([]);
  const [calendarNetPosition, setCalendarNetPosition] = useState<{date:string; electricity_only_gbp:string | null; including_gas_gbp:string | null; electricity_actual?:string | null; electricity_export_actual?:string | null; electricity_standing?:string | null; gas_actual?:string | null; gas_standing?:string | null; octopus_incomplete?:boolean; ecoflow_fallback?:boolean}[]>([]);
  const [calendarNetSummary, setCalendarNetSummary] = useState<Record<string, {electricity_only_gbp:string | null; including_gas_gbp:string | null}> | null>(null);
  const [energyBills, setEnergyBills] = useState<{month:string; octopus_incomplete?:boolean; electricity_kwh:string | null; electricity_actual:string | null; electricity_standing:string | null; electricity_export_kwh:string | null; electricity_export_actual:string | null; electricity_export_standing:string | null; gas_kwh:string | null; gas_actual:string | null; gas_standing:string | null; ecoflow_kwh:string | null; ecoflow_actual:string | null; electricity_only_gbp:string | null; including_gas_gbp:string | null}[]>([]);
  const [energyBillsLoading, setEnergyBillsLoading] = useState(false);
  const [tariffAgreements, setTariffAgreements] = useState<Record<string, {tariff_code:string | null; tariff_start:string | null; tariff_end:string | null; standing_charge_pence:string | null; current_day_average_pence:string | null; historical_average_pence:string | null; variable:boolean}>>({});
  const [calendarRange, setCalendarRange] = useState<"month" | "year">("month");
  const [fiscalStartMonth, setFiscalStartMonth] = useState(1);
  const [calendarLoading, setCalendarLoading] = useState(false);
  const calendarRequestRef = useRef(0);
  const [live, setLive] = useState<Record<string, {value:string; sampled_at:string; age_seconds:number; unit:string}>>(() => {
    try { return JSON.parse(localStorage.getItem("home-energy-live-readings") ?? "{}"); } catch { return {}; }
  });
  const [ecoflowDataRevision, setEcoflowDataRevision] = useState(0);
  const lastTodayOverviewRefreshRef = useRef(0);
  const [livePowerUpdating, setLivePowerUpdating] = useState(false);
  const [miniUpdating, setMiniUpdating] = useState(false);
  const ecoflowSampleAtRef = useRef<string | null>(null);
  const livePowerRefreshTimerRef = useRef<number | null>(null);
  const miniSampleAtRef = useRef<string | null>(null);
  const miniRefreshTimerRef = useRef<number | null>(null);
  useEffect(() => {
    const accept = (metrics: Record<string, {value:string; sampled_at:string; age_seconds:number; unit:string}>) => {
      // Telemetry events can contain only the metrics that changed. Keep the
      // previous readings until each replacement arrives so tiles do not blink
      // out during a refresh.
      setLive((previous) => {
        const next = { ...previous, ...metrics };
        localStorage.setItem("home-energy-live-readings", JSON.stringify(next));
        return next;
      });
      const newestEcoFlowSample = Object.entries(metrics)
        .filter(([key]) => key.startsWith("ecoflow_powerocean:"))
        .map(([, value]) => value.sampled_at)
        .filter((value): value is string => Boolean(value))
        .sort()
        .at(-1);
      if (newestEcoFlowSample && newestEcoFlowSample !== ecoflowSampleAtRef.current) {
        ecoflowSampleAtRef.current = newestEcoFlowSample;
        setLivePowerUpdating(true);
        if (livePowerRefreshTimerRef.current !== null) window.clearTimeout(livePowerRefreshTimerRef.current);
        livePowerRefreshTimerRef.current = window.setTimeout(() => { setLivePowerUpdating(false); livePowerRefreshTimerRef.current = null; }, 450);
        setEcoflowDataRevision((revision) => revision + 1);
      }
      const newestMiniSample = Object.entries(metrics)
        .filter(([key]) => key.startsWith("octopus_mini:"))
        .map(([, value]) => value.sampled_at)
        .filter((value): value is string => Boolean(value))
        .sort()
        .at(-1);
      if (newestMiniSample && newestMiniSample !== miniSampleAtRef.current) {
        miniSampleAtRef.current = newestMiniSample;
        setMiniUpdating(true);
        if (miniRefreshTimerRef.current !== null) window.clearTimeout(miniRefreshTimerRef.current);
        miniRefreshTimerRef.current = window.setTimeout(() => { setMiniUpdating(false); miniRefreshTimerRef.current = null; }, 450);
      }
    };
    const refresh=()=>fetch("/api/telemetry/live",{cache:"no-store"}).then(r=>r.ok?r.json():null).then(v=>accept(v?.metrics??{})).catch(()=>{});
    void refresh(); const events=new EventSource("/api/events");
    events.addEventListener("telemetry",(event)=>{ try { accept(JSON.parse((event as MessageEvent).data).metrics??{}); } catch {} });
    return()=>{ events.close(); if (livePowerRefreshTimerRef.current !== null) window.clearTimeout(livePowerRefreshTimerRef.current); if (miniRefreshTimerRef.current !== null) window.clearTimeout(miniRefreshTimerRef.current); };
  }, []);
  // The selected-date overview loads only when the user changes context.
  // Live MQTT samples do not refresh the other overview panels.
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setOverviewLoading(true);
    fetch("/api/dashboard/overview?date=" + selectedDate + "&details=false", { cache: "no-store", signal: controller.signal })
      .then((response) => response.ok ? response.json() : null)
      .then((value) => { if (active) { setOverview((previous) => ({...(previous ?? {}), ...value})); if (selectedDate === todayDate) setTodayOverview((previous) => ({...(previous ?? {}), ...value})); setOverviewLoading(false); } })
      .catch(() => { if (active) { setOverview(null); if (selectedDate === todayDate) setTodayLoading(false); setOverviewLoading(false); } });
    return () => { active = false; controller.abort(); };
  }, [selectedDate, connection]);

  // Defer the fiscal and settlement aggregates until the summary has painted.
  // They are more expensive than the core current-day metrics and do not need
  // to block the first visible dashboard content.
  useEffect(() => {
    const controller = new AbortController(); let active = true;
    setTodayLoading(true);
    setProjectionLoading(true);
    const timer = window.setTimeout(() => {
      (["today", "net-position", "projection"] as const).forEach((panel) => {
        fetch(`/api/dashboard/overview/details/${panel}?date=${selectedDate}`, { cache: "no-store", signal: controller.signal })
          .then((response) => response.ok ? response.json() : null)
          .then((value) => {
            if (!active || !value) return;
            const merge = (previous: any) => ({...(previous ?? {}), ...value});
            setOverview(merge);
            if (selectedDate === todayDate) setTodayOverview(merge);
            if (panel === "today") setTodayLoading(false);
            if (panel === "projection") setProjectionLoading(false);
          })
          .catch(() => {
            if (!active) return;
            if (panel === "today") setTodayLoading(false);
            if (panel === "projection") setProjectionLoading(false);
          });
      });
    }, 0);
    return () => { active = false; window.clearTimeout(timer); controller.abort(); };
  }, [selectedDate, todayDate, connection]);

  // Only the Today energy panel reacts to new EcoFlow telemetry.
  useEffect(() => {
    if (selectedDate !== todayDate || ecoflowDataRevision === 0) return;
    // Live telemetry arrives about every ten seconds.  The readings update
    // immediately over SSE; recomputing every aggregate panel on each event
    // needlessly repeats the fiscal-year pricing queries.  Refresh the shared
    // overview at most twice a minute while still updating all dependent
    // panels together.
    const now = Date.now();
    if (selectedDate === todayDate && ecoflowDataRevision > 0 && now - lastTodayOverviewRefreshRef.current < 30_000) return;
    lastTodayOverviewRefreshRef.current = now;
    let active = true;
    setTodayLoading(true);
    if (selectedDate === todayDate) setOverviewLoading(true);
    fetch("/api/dashboard/overview?date=" + todayDate, { cache: "no-store" })
      .then((response) => response.ok ? response.json() : null)
      .then((value) => { if (active) { setTodayOverview(value); if (selectedDate === todayDate) { setOverview(value); setOverviewLoading(false); } setTodayLoading(false); } })
      .catch(() => { if (active) { if (selectedDate === todayDate) setOverviewLoading(false); setTodayLoading(false); } });
    return () => { active = false; };
  }, [todayDate, connection, ecoflowDataRevision]);
  useEffect(() => {
    if (activeTab !== "history") return;
    const controller = new AbortController();
    let active = true;
    const refresh = async () => {
      try {
        const response = await fetch("/api/dashboard/history?date=" + historyDate + "&period=" + period, { cache: "no-store", signal: controller.signal });
        const payload = response.ok ? await response.json() : null;
        if (!active) return;
        setHistoryRows(payload?.series ?? []);
        setHistoryCosts(payload?.costs ?? {});
        setHistoryLoading(false);
      } catch {
        if (active) { setHistoryRows([]); setHistoryCosts({}); setHistoryLoading(false); }
      }
    };
    setHistoryLoading(true);
    setHistoryRows([]);
    setHistoryCosts({});
    void refresh();
    const timer = window.setInterval(refresh, period === "day" ? 30_000 : 120_000);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [activeTab, historyDate, period]);
  useEffect(() => {
    if (activeTab !== "vpp") return;
    let active = true;
    const refresh = () => fetch("/api/dashboard/vpp", {cache:"no-store"}).then(async response => {
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(value.detail ?? "Could not load Axle VPP events.");
      return value;
    }).then(value => { if (active) { setVpp(value); setVppError(""); } }).catch(error => { if (active) setVppError(error instanceof Error ? error.message : "Could not load Axle VPP events."); });
    void refresh(); const timer = window.setInterval(refresh, 60_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [activeTab]);
  useEffect(() => { if (!["connections", "imports", "docker-logs"].includes(activeTab)) return; const refresh = () => { setOperationsLoading(true); return fetch("/api/operations", {cache:"no-store"}).then(r => r.ok ? r.json() : null).then(v => { if (v) setOperations(v); }).catch(() => {}).finally(() => setOperationsLoading(false)); }; void refresh(); const timer=window.setInterval(refresh, 5_000); return () => window.clearInterval(timer); }, [activeTab]);
  useEffect(() => {
    if (activeTab !== "docker-logs") return;
    const refresh = () => fetch("/api/collector-logs", {cache:"no-store"}).then(r => r.ok ? r.json() : null).then(v => { if (v?.lines) setCollectorLogs(v.lines); }).catch(() => {});
    void refresh();
    const timer = window.setInterval(refresh, 2_000);
    return () => window.clearInterval(timer);
  }, [activeTab]);
  useEffect(() => {
    if (activeTab !== "history") return;
    const controller = new AbortController();
    const refresh = () => fetch("/api/dashboard/rates?date=" + historyDate, {cache:"no-store", signal:controller.signal}).then((response) => response.ok ? response.json() : null).then((payload) => { setOctopusRates(payload?.rates ?? []); setRatesLoading(false); }).catch(() => setRatesLoading(false));
    setRatesLoading(true); setOctopusRates([]); void refresh();
    const timer = window.setInterval(refresh, 60_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [activeTab, historyDate]);
  useEffect(() => {
    if (activeTab !== "earnings" && activeTab !== "net-position") return;
    const calendarDate = activeTab === "earnings" ? earningsDate : netCalendarDate;
    const year = String(Number(calendarDate.slice(0, 4)) - (calendarRange === "year" && Number(calendarDate.slice(5, 7)) < fiscalStartMonth ? 1 : 0));
    const fiscalYear = calendarRange === "year";
    const fiscalStart = String(fiscalStartMonth).padStart(2, "0");
    const start = calendarRange === "month" ? calendarDate.slice(0, 8) + "01" : fiscalYear ? year + "-" + fiscalStart + "-01" : year + "-01-01";
    const month = Number(calendarDate.slice(5, 7));
    const fiscalEndYear = Number(year) + 1;
    const end = calendarRange === "month" ? `${year}-${String(month).padStart(2, "0")}-${String(new Date(Date.UTC(Number(year), month, 0)).getUTCDate()).padStart(2, "0")}` : fiscalYear ? `${fiscalStartMonth === 1 ? Number(year) : fiscalEndYear}-${String(fiscalStartMonth === 1 ? 12 : fiscalStartMonth - 1).padStart(2, "0")}-${String(new Date(Date.UTC(fiscalStartMonth === 1 ? Number(year) : fiscalEndYear, fiscalStartMonth - 1, 0)).getUTCDate()).padStart(2, "0")}` : year + "-12-31";
    const requestId = ++calendarRequestRef.current;
    setCalendarLoading(true);
    if (activeTab === "earnings") fetch("/api/dashboard/daily-earnings?start=" + start + "&end=" + end + "&request=" + requestId, {cache:"no-store"}).then(r => r.ok ? r.json() : null).then(async v => {
      const rows = v?.rows ?? [];
      if (calendarRequestRef.current === requestId) { setCalendarEarnings(rows); if (Number.isInteger(Number(v?.fiscal_start_month)) && Number(v.fiscal_start_month) >= 1 && Number(v.fiscal_start_month) <= 12) setFiscalStartMonth(Number(v.fiscal_start_month)); }
    }).catch(() => { if (calendarRequestRef.current === requestId) setCalendarEarnings([]); }).finally(() => { if (calendarRequestRef.current === requestId) setCalendarLoading(false); });
    if (activeTab === "net-position") fetch("/api/dashboard/daily-net-position?start=" + start + "&end=" + end + "&as_of=" + netCalendarDate + "&request=" + requestId, {cache:"no-store"}).then(r => r.ok ? r.json() : null).then(v => { if (calendarRequestRef.current === requestId) { setCalendarNetPosition(v?.rows ?? []); setCalendarNetSummary(v?.summary ?? null); if (Number.isInteger(Number(v?.fiscal_start_month)) && Number(v.fiscal_start_month) >= 1 && Number(v.fiscal_start_month) <= 12) setFiscalStartMonth(Number(v.fiscal_start_month)); } }).catch(() => { if (calendarRequestRef.current === requestId) { setCalendarNetPosition([]); setCalendarNetSummary(null); } }).finally(() => { if (calendarRequestRef.current === requestId) setCalendarLoading(false); });
  }, [activeTab, earningsDate, netCalendarDate, calendarRange, fiscalStartMonth]);
  useEffect(() => {
    if (activeTab !== "bills") return;
    const controller = new AbortController();
    setEnergyBillsLoading(true);
    fetch("/api/dashboard/energy-bills?year=" + billsDate.slice(0, 4), {cache:"no-store", signal:controller.signal})
      .then(response => response.ok ? response.json() : null)
      .then(payload => { setEnergyBills(payload?.rows ?? []); setTariffAgreements(payload?.agreements ?? {}); setEnergyBillsLoading(false); })
      .catch(() => { if (!controller.signal.aborted) { setEnergyBills([]); setTariffAgreements({}); setEnergyBillsLoading(false); } });
    return () => controller.abort();
  }, [activeTab, billsDate]);
  useEffect(() => {
    if (activeTab !== "tariff-comparison") return;
    let active = true;
    fetch("/api/dashboard/tariff-comparison/defaults", {cache:"no-store"})
      .then(response => response.ok ? response.json() : null)
      .then(value => {
        if (!active || !value) return;
        if (!tariffCandidates.some(candidate => candidate.import_tariff_code || candidate.export_tariff_code)) setTariffCandidates([{name:"Current tariff", import_tariff_code:value.import_tariff_code ?? "", export_tariff_code:value.export_tariff_code ?? ""}]);
        setTariffCodeOptions({import_options:value.import_options ?? [], export_options:value.export_options ?? []});
        setTariffBatteryCapacity(value.battery_capacity_kwh ?? null);
      }).catch(() => {});
    return () => { active = false; };
  }, [activeTab]);
  const handleSelectedDateChange = (value: string) => {
    setOverviewLoading(true); setHistoryLoading(true); setRatesLoading(true);
    setHistoryRows([]); setHistoryCosts({}); setOctopusRates([]);
    setSelectedDate(value);
  };
  const selectTab = (tab: DashboardTab) => {
    setActiveTab(tab);
    setMobileNavOpen(false);
    requestAnimationFrame(() => document.getElementById("main")?.focus());
  };
  const compareTariffs = async () => {
    setTariffComparisonLoading(true); setTariffComparisonError(""); setTariffComparisonResults([]); setTariffComparisonSummary(null); setTariffComparisonInsights([]); setTariffComparisonDaily([]);
    try {
      const response = await fetch("/api/dashboard/tariff-comparison", {method:"POST", headers:{"Content-Type":"application/json", "X-CSRF-Token":csrf()}, body:JSON.stringify({start:tariffComparisonStart, end:tariffComparisonEnd, candidates:tariffCandidates, battery_dispatch:{enabled:tariffBatteryDispatchEnabled, discharge_percent:tariffBatteryDischargePercent, start_time:tariffBatteryDischargeStart, end_time:tariffBatteryDischargeEnd}})});
      const value = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(value.detail ?? "Could not compare those tariffs.");
      setTariffComparisonResults(value.candidates ?? []);
      setTariffComparisonSummary({settled_import_kwh:value.settled_import_kwh, settled_export_kwh:value.settled_export_kwh, simulated_export_kwh:value.simulated_export_kwh ?? "0"});
      setTariffComparisonInsights(value.insights?.usage_periods ?? []);
      setTariffComparisonDaily(value.daily ?? []);
    } catch (error) { setTariffComparisonError(error instanceof Error ? error.message : "Could not compare those tariffs."); }
    finally { setTariffComparisonLoading(false); }
  };
  const updateTariffCandidateCode = (index: number, field: "import_tariff_code" | "export_tariff_code", rawValue: string) => {
    const value = rawValue.toUpperCase();
    setTariffCandidates(rows => rows.map((row, rowIndex) => {
      if (rowIndex !== index) return row;
      const partner = field === "import_tariff_code"
        ? value.replace("FLUX-IMPORT-", "FLUX-EXPORT-")
        : value.replace("FLUX-EXPORT-", "FLUX-IMPORT-");
      const isFlux = partner !== value;
      return field === "import_tariff_code"
        ? {...row, import_tariff_code:value, ...(isFlux ? {export_tariff_code:partner} : {})}
        : {...row, export_tariff_code:value, ...(isFlux ? {import_tariff_code:partner} : {})};
    }));
  };
  const openCalendarDetail = (day: string) => {
    if (window.matchMedia("(max-width: 767px)").matches) setCalendarDetailDate(day);
  };
  const reading = (key: string, unit = "kWh") => { const value = live[key]?.value ?? overview?.live[key]?.value ?? overview?.energy[key]; return value === undefined ? "—" : (unit === "W" ? (Number(value) / 1000).toFixed(2) : (unit === "%" || unit === "count") ? String(Math.round(Number(value))) : Number(value).toFixed(3)); };
  const money = (value: string | null | undefined) => value === null || value === undefined ? "—" : new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(Number(value));
  const calendarValue = (value: string | null | undefined) => value === null || value === undefined ? "—" : Number(value).toFixed(2);
  const pence = (value: string | null | undefined) => value === null || value === undefined ? "—" : Number(value).toFixed(2) + "p";
  const agreementDate = (value: string | null | undefined) => value === null || value === undefined ? "Ongoing" : new Intl.DateTimeFormat("en-GB", { day:"numeric", month:"short", year:"numeric", timeZone:"Europe/London" }).format(new Date(value));
  const historyNetPosition = (() => {
    const values = [historyCosts.import_cost_gbp, historyCosts.export_credit_gbp, historyCosts.gas_cost_gbp];
    if (values.some((value) => value === null || value === undefined || !Number.isFinite(Number(value)))) return null;
    return String(Number(historyCosts.export_credit_gbp) - Number(historyCosts.import_cost_gbp) - Number(historyCosts.gas_cost_gbp));
  })();
  const historyExportFallback = historyCosts.export_fallback === "true";
  const historyNetFallback = historyCosts.net_fallback === "true" && historyNetPosition !== null;
  const selectedMonthLabel = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "Europe/London" }).format(new Date(`${selectedDate}T12:00:00Z`));
  const netPositionPeriods = [
    ["day", "Day"],
    ["month", "Month"],
    ["fiscal_year", "Fiscal year"],
    ["lifetime", "Lifetime"],
  ] as const;
  const selectedNetPosition = overview?.net_position?.[netPositionPeriod];
  const selectedNetPositionLabel = netPositionPeriod === "month" ? `Month (${selectedMonthLabel})` : netPositionPeriod === "fiscal_year" ? `This fiscal year${overview?.periods?.year_label ? ` (${overview.periods.year_label})` : ""}` : netPositionPeriod === "lifetime" ? "Lifetime" : "Today";
  const selectedNetPositionFallback = netPositionPeriod === "day" && Boolean(selectedNetPosition?.ecoflow_fallback);
  const liveConnected = (key:string) => live[key] !== undefined || overview?.live[key] !== undefined;
  const liveStatus = (key: string, fallback = "Not reported") => {
    const value = live[key]?.value ?? overview?.live[key]?.value;
    return value === undefined || value === null || value === "" || value === "None" ? fallback : String(value);
  };
  const energyNumber = (metric: string) => Number(todayOverview?.energy[`ecoflow_powerocean:${metric}`]);
  const summaryReading = (metric: string) => {
    const value = metric === "solar_generation_energy"
      ? todayOverview?.periods?.day_kwh ?? todayOverview?.energy[`ecoflow_powerocean:${metric}`]
      : todayOverview?.energy[`ecoflow_powerocean:${metric}`];
    return value === undefined || value === null ? "—" : Number(value).toFixed(3);
  };
  const periodReading = (key: string) => {
    const value = todayOverview?.periods?.[key];
    return value === null || value === undefined ? "—" : Number(value).toFixed(3);
  };
  // Preserve the last live readings while a newer EcoFlow summary is fetched.
  const summaryUpdating = todayLoading || (selectedDate === todayDate && overviewLoading);
  const summaryLoading = false;
  const dailyPeriod = {
    month: { label: "This month", kwh: "month_kwh", value: todayOverview?.periods?.month_gbp, earnings: todayOverview?.periods?.month_total_earnings_gbp },
    year: { label: `This year${todayOverview?.periods?.year_label ? ` (${todayOverview.periods.year_label})` : ""}`, kwh: "year_kwh", value: todayOverview?.periods?.year_gbp, earnings: todayOverview?.periods?.year_total_earnings_gbp },
    lifetime: { label: "Lifetime", kwh: "lifetime_kwh", value: todayOverview?.periods?.lifetime_gbp, earnings: todayOverview?.periods?.lifetime_total_earnings_gbp },
  }[dailySummaryPeriod];
  const reportedIndependence = todayOverview?.energy_independence_percent;
  const appEnergyIndependence = reportedIndependence === null || reportedIndependence === undefined
    ? Number.NaN
    : Number(reportedIndependence);
  const energyIndependence = Number.isFinite(appEnergyIndependence)
    ? appEnergyIndependence
    : Number.isFinite(energyNumber("ecoflow_to_home")) && energyNumber("ecoflow_to_home") > 0
      ? Math.max(0, Math.min(100, (1 - energyNumber("grid_import_energy") / energyNumber("ecoflow_to_home")) * 100))
      : NaN;
  const miniRegister = (metric: string) => { const value = Number(live[`octopus_mini:${metric}`]?.value); return Number.isFinite(value) ? (value / 1000).toFixed(3) : "—"; };
  const miniLive = Object.keys(live).some((key) => key.startsWith("octopus_mini:"));
  const gridWatts = Number(live["ecoflow_powerocean:grid_power"]?.value);
  // PowerOcean grid power is signed (+ import, − export). Suppress its small
  // near-zero oscillation so the panel matches the EcoFlow app's idle state.
  const GRID_IDLE_DEADBAND_W = 50;
  const gridFlow = Number.isFinite(gridWatts) && Math.abs(gridWatts) >= GRID_IDLE_DEADBAND_W
    ? {
        value: (Math.abs(gridWatts) / 1000).toFixed(2),
        label: gridWatts > 0 ? "Importing" : "Exporting",
        Icon: gridWatts > 0 ? ArrowDownLeft : ArrowUpRight,
      }
    : null;
  const soc = Number(live["ecoflow_powerocean:battery_soc"]?.value);
  const batteryWatts = Number(live["ecoflow_powerocean:battery_power"]?.value);
  const batteryIsFull = Number.isFinite(soc) && Math.round(soc) >= 100;
  // PowerOcean reports negative battery power while discharging.
  const batteryFlow = Number.isFinite(batteryWatts) && batteryWatts !== 0
    ? {
        value: (Math.abs(batteryWatts) / 1000).toFixed(2),
        label: batteryWatts > 0 ? "Charging" : "Discharging",
        Icon: batteryWatts > 0 ? ArrowDownLeft : ArrowUpRight,
      }
    : null;
  const wholeNumber = (value: unknown) => {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? String(Math.round(numeric)) : "—";
  };
  const packs = Object.entries(live).filter(([key]) => key.endsWith("_serial") && key.startsWith("ecoflow_powerocean:ecoflow_pack_")).map(([key, entry]) => {
    const prefix = key.slice(0, -"_serial".length);
    const field = (name: string) => live[`${prefix}_${name}`]?.value;
    // Named BMS temperatures mirror the PowerOcean parser in ecoflow-energy-ha.
    const temperature = ["env_temp_c", "max_cell_temp_c", "min_cell_temp_c", "max_mos_temp_c", "hv_mos_temp_c", "lv_mos_temp_c", "ptc_temp_c", "temperature"]
      .map(field).find((value) => Number.isFinite(Number(value)));
    const capacityWh = Number(field("capacity_wh"));
    return { serial: String(entry.value), soc: wholeNumber(field("soc")), soh: wholeNumber(field("soh")), cycles: wholeNumber(field("cycles")), temperature: wholeNumber(temperature), capacity: Number.isFinite(capacityWh) ? `${(capacityWh / 1000).toFixed(2)} kWh` : "—" };
  });
  const displayedPacks = packs;
  const totalBatteryCapacityWh = Number(live["ecoflow_powerocean:ecoflow_battery_capacity_wh"]?.value);
  const totalBatteryCapacity = Number.isFinite(totalBatteryCapacityWh) ? `${(totalBatteryCapacityWh / 1000).toFixed(2)} kWh` : "—";
  const diagnostics = (overview as any)?.diagnostics ?? [];
  const reachable = connection === "connected";
  // Keep the last confirmed provider status visible while SSE reconnects.
  // A transport blip must not misrepresent a failed import as “not connected”.
  const collector = status?.collector.state;
  const collectorLabel =
    collector === "healthy"
      ? "Running"
      : collector === "stale"
        ? "Stale"
        : collector === "waiting"
          ? "Waiting"
          : "Unknown";
  const databaseReady = status?.database === "ready";
  const octopus = status?.providers.octopus;
  const gas = status?.providers.octopus_gas;
  const ecoflow = status?.providers.ecoflow;
  const mini = status?.providers.octopus_mini;
  const providerLabel = (provider: typeof ecoflow) => provider?.state === "healthy" ? "Imported" : provider?.state === "backfilling" ? "Backfilling" : provider?.state === "syncing" ? "Syncing" : provider?.state === "partial" ? "Incomplete" : provider?.state === "failed" ? "Needs attention" : "Not connected";
  const retryPartialImports = async () => {
    setRetryingPartials(true);
    try { await fetch("/api/operations/retry-partials", {method:"POST", headers:{"X-CSRF-Token":csrf()}}); }
    finally { setRetryingPartials(false); }
  };
  const importHelp = (provider: typeof ecoflow, source: string) => {
    if (!provider || provider.state === "healthy") return undefined;
    if (provider.state === "partial") return `${source} is connected and importing. Some historic coverage windows are incomplete; the collector retries them automatically. Check Imports for the affected windows or run: docker compose -f production-compose.yaml logs --tail=100 collector`;
    if (provider.state === "backfilling") return `${source} is connected and the initial history import is still running. Leave the collector running; follow progress in Imports or with: docker compose -f production-compose.yaml logs --tail=100 collector`;
    if (provider.state === "syncing") return `${source} is connecting or waiting for fresh data. If this does not change after a few minutes, inspect: docker compose -f production-compose.yaml logs --tail=100 collector`;
    return `${provider.reason ?? `${source} is unavailable.`} Inspect the collector logs with: docker compose -f production-compose.yaml logs --tail=100 collector`;
  };
  const octopusLabel = providerLabel(octopus);
  const observedAt = status
    ? new Date(status.observed_at).toLocaleTimeString("en-GB", {
        timeZone: "Europe/London",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";
  const day = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London",
    weekday: "long",
    day: "numeric",
    month: "long",
  }).format(new Date());
  const selectedDateLabel = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).format(new Date(selectedDate + "T12:00:00Z"));
  const historyDateLabel = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London", weekday: "long", day: "numeric", month: "long", year: "numeric",
  }).format(new Date(historyDate + "T12:00:00Z"));
  const calendarDate = activeTab === "net-position" ? netCalendarDate : earningsDate;
  const calendarDays = calendarRange === "month" ? Array.from({length: new Date(Number(calendarDate.slice(0, 4)), Number(calendarDate.slice(5, 7)), 0).getDate()}, (_, index) => calendarDate.slice(0, 8) + String(index + 1).padStart(2, "0")) : Array.from({length: new Date(Number(calendarDate.slice(0, 4)) + 1, 0, 0).getDate()}, () => calendarDate.slice(0, 4) + "-01-01").map((start, index) => new Date(new Date(start + "T12:00:00Z").getTime() + index * 86400000).toISOString().slice(0, 10));
  const calendarWeekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
  const calendarLeadingDays = calendarRange === "month" ? new Date(calendarDate.slice(0, 8) + "01T12:00:00Z").getUTCDay() : 0;
  const calendarMonths = Array.from({length: 12}, (_, index) => String(index + 1).padStart(2, "0"));
  const calendarFiscalYear = Number(calendarDate.slice(0, 4)) - (Number(calendarDate.slice(5, 7)) < fiscalStartMonth ? 1 : 0);
  const fiscalYearLabel = `${String(fiscalStartMonth).padStart(2, "0")}/${calendarFiscalYear} – ${String(fiscalStartMonth === 1 ? 12 : fiscalStartMonth - 1).padStart(2, "0")}/${fiscalStartMonth === 1 ? calendarFiscalYear : calendarFiscalYear + 1}`;
  const fiscalMonthSlots = Array.from({length: Math.ceil((fiscalStartMonth - 1 + 12) / 4) * 4}, (_, index) => {
    const monthOffset = index - (fiscalStartMonth - 1);
    if (monthOffset < 0 || monthOffset >= 12) return null;
    const monthNumber = ((fiscalStartMonth - 1 + monthOffset) % 12) + 1;
    return { month: String(monthNumber).padStart(2, "0"), year: calendarFiscalYear + (monthNumber < fiscalStartMonth ? 1 : 0) };
  });
  const changeCalendarYear = (year: string, _currentDate: string, setDate: (value: string) => void) => { if (/^[0-9]{4}$/.test(year)) setDate(`${year}-${String(fiscalStartMonth).padStart(2, "0")}-01`); };
  const earningsByDay = Object.fromEntries(calendarEarnings.map(row => [row.date, row]));
  const netPositionByDay = Object.fromEntries(calendarNetPosition.map(row => [row.date, row]));
  const totalIfPresent = (values: Array<string | null | undefined>) => {
    const numbers = values.filter((value): value is string => value !== null && value !== undefined).map(Number).filter(Number.isFinite);
    return numbers.length ? String(numbers.reduce((sum, value) => sum + value, 0)) : null;
  };
  const netCalendarTotals = (rows: typeof calendarNetPosition) => {
    const imported = totalIfPresent(rows.map(row => row.electricity_actual));
    const exported = totalIfPresent(rows.map(row => row.electricity_export_actual));
    const standing = totalIfPresent(rows.map(row => row.electricity_standing));
    const gas = totalIfPresent(rows.map(row => row.gas_actual));
    const gasStanding = totalIfPresent(rows.map(row => row.gas_standing));
    const electricity = (imported === null && exported === null) || standing === null ? null : String(Number(exported ?? 0) - Number(imported ?? 0) - Number(standing));
    return {electricity, gas: electricity === null || gas === null || gasStanding === null ? null : String(Number(electricity) - Number(gas) - Number(gasStanding))};
  };
  const selectedNetTotals = netCalendarTotals(calendarNetPosition);
  const dailyNetTrend = calendarDays.map(day => Number(netPositionByDay[day]?.including_gas_gbp)).filter(Number.isFinite);
  const highestDailyNetMagnitude = Math.max(0, ...dailyNetTrend.map(value => Math.abs(value)));
  const fiscalNetMonths = fiscalMonthSlots.filter((slot): slot is {month:string; year:number} => slot !== null).map(slot => {
    const rows = calendarNetPosition.filter(row => row.date.slice(0, 7) === `${slot.year}-${slot.month}`);
    return { ...slot, ...netCalendarTotals(rows), incomplete: rows.some(row => row.octopus_incomplete), fallback: rows.some(row => row.ecoflow_fallback) };
  });
  const highestNetMagnitude = Math.max(0, ...fiscalNetMonths.map(month => Math.abs(Number(month.gas))).filter(Number.isFinite));
  const selectedEarningsTotal = totalIfPresent(calendarEarnings.map(row => row.earnings_gbp));
  const highestDailyEarnings = Math.max(0, ...calendarEarnings.map(row => Number(row.earnings_gbp)).filter(Number.isFinite));
  const dailyEarningsTrend = calendarDays.map(day => Math.max(0, Number(earningsByDay[day]?.earnings_gbp) || 0));
  const fiscalEarningMonths = fiscalMonthSlots.filter((slot): slot is {month:string; year:number} => slot !== null).map(slot => {
    const rows = calendarEarnings.filter(row => row.date.slice(0, 7) === `${slot.year}-${slot.month}`);
    return { ...slot, total: totalIfPresent(rows.map(row => row.earnings_gbp)), incomplete: rows.some(row => row.octopus_incomplete) };
  });
  const highestFiscalEarnings = Math.max(0, ...fiscalEarningMonths.map(month => Number(month.total)).filter(Number.isFinite));
  const billTotal = (key: "electricity_actual" | "electricity_standing" | "electricity_export_actual" | "gas_actual" | "gas_standing" | "ecoflow_actual") => totalIfPresent(energyBills.map(row => row[key]));
  const moneySum = (...values: Array<string | null | undefined>) => values.some(value => value === null || value === undefined) ? null : String(values.reduce((sum, value) => sum + Number(value), 0));
  const kwh = (value: string | null | undefined) => value === null || value === undefined ? "—" : Number(value).toFixed(3) + " kWh";
  const comparableTariffs = tariffComparisonResults.filter(result => result.reason === null && result.net_position_gbp !== null).sort((left, right) => Number(right.net_position_gbp) - Number(left.net_position_gbp));
  const bestTariff = comparableTariffs[0];
  const baselineTariff = tariffComparisonResults[0];
  const bestTariffDifference = bestTariff && baselineTariff?.net_position_gbp != null && bestTariff.net_position_gbp !== null ? Number(bestTariff.net_position_gbp) - Number(baselineTariff.net_position_gbp) : null;

  return (
    <>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="topbar">
        <a className="brand" href="#main" aria-label="Home Energy overview">
          <span className="brand-mark">
            <Zap size={22} fill="currentColor" />
          </span>
          <span>
            home<span className="brand-light">energy</span>
            <small>YOUR HOME, IN BALANCE</small>
          </span>
        </a>
        <button type="button" className="mobile-menu-button" aria-label="Toggle navigation menu" aria-expanded={mobileNavOpen} onClick={() => setMobileNavOpen((open) => !open)}>{mobileNavOpen ? <X size={22} /> : <Menu size={22} />}</button>
        <nav className={mobileNavOpen ? "mobile-open" : ""} aria-label="Main navigation">
          <button className={activeTab === "overview" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "overview" ? "page" : undefined} onClick={() => selectTab("overview")}><Grid2X2 size={16} />Overview</button>
          <button className={activeTab === "earnings" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "earnings" ? "page" : undefined} onClick={() => selectTab("earnings")}><Coins size={16} />Earnings</button>
          <button className={activeTab === "net-position" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "net-position" ? "page" : undefined} onClick={() => selectTab("net-position")}><ArrowUpRight size={16} />Net</button>
          <button className={activeTab === "bills" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "bills" ? "page" : undefined} onClick={() => selectTab("bills")}><Coins size={16} />Bills</button>
          <button className={activeTab === "history" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "history" ? "page" : undefined} onClick={() => selectTab("history")}><Activity size={16} />History</button>
          <button className={activeTab === "system" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "system" ? "page" : undefined} onClick={() => selectTab("system")}><Battery size={16} />System</button>
          <button className={activeTab === "vpp" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "vpp" ? "page" : undefined} onClick={() => selectTab("vpp")}><Zap size={16} />VPP</button>
          <button className={activeTab === "tariff-comparison" ? "nav-link active" : "nav-link"} type="button" aria-current={activeTab === "tariff-comparison" ? "page" : undefined} onClick={() => selectTab("tariff-comparison")}><ReceiptPoundSterling size={16} />Compare</button>
          {onSwitchDashboard && <button className="nav-link" type="button" onClick={onSwitchDashboard}><ArrowLeftRight size={16} />Boards</button>}
          {onSettings && <button className="nav-link" type="button" onClick={onSettings}><Settings size={16} />Settings</button>}
          {onLogout && <button className="nav-link" type="button" onClick={onLogout}><LogOut size={16} />Log out</button>}
        </nav>

      </header>

      <main id="main" className={`tab-page tab-${activeTab}`} tabIndex={-1}>
        <section className={activeTab !== "tariff-comparison" ? "page-heading tab-hidden" : "page-heading"}>
          <div><div className="eyebrow">HOME ENERGY / TARIFF COMPARISON</div><h1>Compare your real energy.<br />Find the best tariff fit.</h1><p>Price your settled Octopus import and export intervals against tariff combinations using the dashboard’s existing Octopus connection.</p></div>
        </section>
        <section className={activeTab !== "tariff-comparison" ? "card tariff-comparison-panel tab-hidden" : "card tariff-comparison-panel"} aria-labelledby="tariff-comparison-heading">
          <div className="panel-heading"><div><h2 id="tariff-comparison-heading"><ArrowLeftRight size={20} />Octopus tariff comparison</h2><p>Uses your actual settled meter profile. Choose up to five import and export tariff combinations.</p></div></div>
          <div className="tariff-comparison-controls"><label><span><CalendarDays size={15} />From</span><input type="date" value={tariffComparisonStart} max={tariffComparisonEnd} onChange={event => setTariffComparisonStart(event.target.value)} /></label><label><span><CalendarDays size={15} />To</span><input type="date" value={tariffComparisonEnd} min={tariffComparisonStart} max={todayDate} onChange={event => setTariffComparisonEnd(event.target.value)} /></label><button type="button" onClick={() => void compareTariffs()} disabled={tariffComparisonLoading}><Scale size={16} />{tariffComparisonLoading ? "Comparing…" : "Compare tariffs"}</button></div>
          <p className="tariff-date-range-help">The date range is the historic settled meter profile being repriced. The end date is exclusive, so 13 Sep–13 Oct covers 30 days.</p>
          <fieldset className="tariff-comparison-settings"><legend><Battery size={17} />Comparison settings</legend><label className="tariff-dispatch-toggle"><input type="checkbox" checked={tariffBatteryDispatchEnabled} onChange={event => setTariffBatteryDispatchEnabled(event.target.checked)} />Apply evening battery discharge to the comparison</label><div aria-disabled={!tariffBatteryDispatchEnabled}><label><span>Battery capacity</span><output className="tariff-battery-capacity">{tariffBatteryCapacity === null ? "Not reported by MQTT" : `${Number(tariffBatteryCapacity).toFixed(2)} kWh`}</output><small>{tariffBatteryCapacity === null ? "EcoFlow capacity telemetry is unavailable" : "Reported by EcoFlow · read-only"}</small></label><label><span>Battery discharge</span><input type="number" min="0" max="100" step="1" value={tariffBatteryDischargePercent} disabled={!tariffBatteryDispatchEnabled || tariffBatteryCapacity === null} onChange={event => setTariffBatteryDischargePercent(event.target.value)} /><small>% of capacity released across this range</small></label><label><span>From</span><input type="time" value={tariffBatteryDischargeStart} disabled={!tariffBatteryDispatchEnabled || tariffBatteryCapacity === null} onChange={event => setTariffBatteryDischargeStart(event.target.value)} /><small>Start time</small></label><label><span>To</span><input type="time" value={tariffBatteryDischargeEnd} disabled={!tariffBatteryDispatchEnabled || tariffBatteryCapacity === null} onChange={event => setTariffBatteryDischargeEnd(event.target.value)} /><small>End time</small></label></div><p>{tariffBatteryDispatchEnabled ? "Each day, the comparison releases the selected share of EcoFlow’s reported capacity evenly across this window. It first reduces settled grid imports, then treats any surplus as grid export at each candidate tariff’s active export rate. It does not simulate charging, efficiency, or degradation." : "Optional default plan: assume a 100% battery, then release 50% of EcoFlow’s reported capacity from 17:00 to 19:00. Tick the box to include it in the comparison."}</p></fieldset>
          <div className="tariff-candidate-list">{tariffCandidates.map((candidate, index) => <article className="tariff-candidate" key={index}><label>Name<input value={candidate.name} maxLength={80} onChange={event => setTariffCandidates(rows => rows.map((row, rowIndex) => rowIndex === index ? {...row, name:event.target.value} : row))} /></label><label><span><ArrowDownLeft size={15} />Import tariff code</span><input list="tariff-import-options" value={candidate.import_tariff_code} maxLength={160} placeholder="E-1R-…" onChange={event => updateTariffCandidateCode(index, "import_tariff_code", event.target.value)} /></label><label><span><ArrowUpRight size={15} />Export tariff code</span><input list="tariff-export-options" value={candidate.export_tariff_code} maxLength={160} placeholder="E-1R-OUTGOING-…" onChange={event => updateTariffCandidateCode(index, "export_tariff_code", event.target.value)} /></label>{tariffCandidates.length > 1 && <button type="button" className="remove-tariff-candidate" onClick={() => setTariffCandidates(rows => rows.filter((_, rowIndex) => rowIndex !== index))}>Remove</button>}</article>)}<datalist id="tariff-import-options">{tariffCodeOptions.import_options.map(option => <option key={option.code} value={option.code}>{option.label}</option>)}</datalist><datalist id="tariff-export-options">{tariffCodeOptions.export_options.map(option => <option key={option.code} value={option.code}>{option.label}</option>)}</datalist></div>
          {tariffCandidates.length < 5 && <button type="button" className="secondary-button add-tariff-candidate" onClick={() => setTariffCandidates(rows => [...rows, {name:`Option ${rows.length + 1}`, import_tariff_code:"", export_tariff_code:""}])}><Plus size={16} />Add tariff combination</button>}
          {tariffComparisonError && <p className="setup-error" role="alert">{tariffComparisonError}</p>}
          {tariffComparisonSummary && <p className="tariff-comparison-summary">Settled profile used: <b>{Number(tariffComparisonSummary.settled_import_kwh).toFixed(3)} kWh imported</b> and <b>{Number(tariffComparisonSummary.settled_export_kwh).toFixed(3)} kWh exported</b>{Number(tariffComparisonSummary.simulated_export_kwh) > 0 && <> · <b>{Number(tariffComparisonSummary.simulated_export_kwh).toFixed(3)} kWh simulated battery export</b></>}.</p>}
          {tariffComparisonResults.length > 0 && <div className="tariff-result-grid">{tariffComparisonResults.map((result, index) => <article key={index} className="tariff-result"><h3>{result.name || "Tariff option"}</h3><small>{result.import_tariff_code} / {result.export_tariff_code}</small>{result.reason ? <p className="tariff-result-error">{result.reason}</p> : <><div><span>Import cost</span><strong>{money(result.import_cost_gbp)}</strong></div><div><span>Export credit</span><strong>{money(result.export_credit_gbp)}</strong></div><div><span>Standing charge</span><strong>{money(result.standing_charge_gbp)}</strong></div><div className="tariff-result-net"><span>Net position</span><b>{money(result.net_position_gbp)}</b></div></>}</article>)}</div>}
          {tariffComparisonResults.length > 0 && <section className="tariff-insights" aria-labelledby="tariff-insights-heading">
            <div className="tariff-insights-heading"><div><h3 id="tariff-insights-heading">Profile insights</h3><p>Based on the settled intervals in this comparison.</p></div>{bestTariff && <div className="tariff-best-fit"><span>Best result</span><strong>{bestTariff.name}</strong><small>{bestTariffDifference !== null && baselineTariff && bestTariff !== baselineTariff ? `${money(String(Math.abs(bestTariffDifference)))} ${bestTariffDifference >= 0 ? "better than" : "behind"} ${baselineTariff.name}` : money(bestTariff.net_position_gbp)}</small></div>}</div>
            {tariffComparisonInsights.length > 0 && <div className="tariff-usage-periods">{tariffComparisonInsights.map(period => <article key={period.key}><div><span>{period.label}</span><b>{Number(period.percent).toFixed(1)}%</b></div><div className="tariff-usage-track"><i style={{width:`${Math.max(0, Math.min(100, Number(period.percent)))}%`}} /></div><small>{Number(period.kwh).toFixed(3)} kWh · {period.hours}</small></article>)}</div>}
            {tariffComparisonDaily.length > 0 && <details className="tariff-drilldown"><summary>Drill down by day <span>See import, export, and each tariff’s daily net position</span></summary><div className="tariff-drilldown-scroll"><table><thead><tr><th>Date</th><th>Import</th><th>Export</th>{tariffComparisonResults.map((result, index) => <th key={index}>{result.name || `Option ${index + 1}`}</th>)}</tr></thead><tbody>{tariffComparisonDaily.map(day => <tr key={day.date}><td>{new Intl.DateTimeFormat("en-GB", {weekday:"short", day:"numeric", month:"short"}).format(new Date(day.date + "T12:00:00Z"))}</td><td>{Number(day.import_kwh).toFixed(3)} kWh</td><td>{Number(day.export_kwh).toFixed(3)} kWh</td>{tariffComparisonResults.map((_, index) => <td key={index}>{money(day.candidates[index]?.net_position_gbp)}</td>)}</tr>)}</tbody></table></div></details>}
          </section>}
          <Note className="finance-note">This comparison prices each settled import and export interval at the candidate tariff’s rate active at that time. {tariffBatteryDispatchEnabled ? "Battery dispatch releases the selected percentage of EcoFlow’s reported capacity once per day, distributed across the selected window. It reduces recorded import first, then values any surplus as export; it does not simulate charging, efficiency, or degradation." : "With evening battery dispatch off, it preserves your recorded import and export behaviour."}</Note>
        </section>
        <section className={activeTab !== "earnings" ? "page-heading tab-hidden" : "page-heading"}>
          <div><div className="eyebrow">HOME ENERGY / EARNINGS</div><h1>Total earnings.</h1><p>Track EcoFlow savings and export yield using your configured rates.</p></div>
        </section>
        <section className={activeTab !== "earnings" ? "card finance-page tab-hidden" : "card finance-page"} aria-labelledby="earnings-page-heading">
          <div className="panel-heading"><div><h2 id="earnings-page-heading">Earnings calendar</h2><p>EcoFlow total earnings using configured rates.</p></div><div className="calendar-controls"><button type="button" className={calendarRange === "month" ? "active" : ""} onClick={() => setCalendarRange("month")}>Month</button><button type="button" className={calendarRange === "year" ? "active" : ""} onClick={() => setCalendarRange("year")}>Year</button>{calendarRange === "month" ? <label className="period-chip">Month <input aria-label="Earnings calendar month" type="month" value={earningsDate.slice(0, 7)} onChange={(event) => setEarningsDate(event.target.value + "-01")} /></label> : <label className="period-chip">Year <input aria-label="Earnings calendar year" type="number" min="2000" max="2100" value={calendarFiscalYear} onChange={(event) => changeCalendarYear(event.target.value, earningsDate, setEarningsDate)} /></label>}</div></div>
          <div className="earnings-glance-grid">
            <article className="earnings-glance earnings-glance-current">
              <div><span>{calendarRange === "month" ? "This month" : `Fiscal year (${fiscalYearLabel})`}</span><strong>{calendarLoading ? <span className="loading-spinner" /> : money(selectedEarningsTotal)}</strong><small>earned so far</small></div>
              {calendarRange === "month" && <div className="earnings-trend" aria-label="Daily earnings trend">
                {dailyEarningsTrend.map((value, index) => <i key={calendarDays[index]} style={{ height: `${highestDailyEarnings > 0 ? Math.max(12, (value / highestDailyEarnings) * 100) : 12}%` }} />)}
              </div>}
            </article>
            <article className="earnings-glance earnings-glance-lifetime">
              <div><span>Lifetime</span><strong>{money(todayOverview?.periods?.lifetime_calendar_earnings_gbp)}</strong><small>earned</small></div>
              <div className="earnings-generated"><b>{kwh(todayOverview?.periods?.lifetime_kwh)}</b><span>generated</span></div>
            </article>
          </div>
          {calendarRange === "year" && <p className="earnings-bar-scale">Each bar is relative to the highest-earning month in this fiscal year ({money(highestFiscalEarnings.toString())}).</p>}
          <div className={calendarRange === "month" ? "calendar-grid earnings-month-grid" : "earnings-year-grid"} aria-label="Total earnings calendar">
            {calendarLoading && <span className="calendar-loading" aria-label="Loading earnings calendar"><span className="loading-spinner" /></span>}
            {calendarRange === "month" ? <>
              <div className="calendar-weekdays">{calendarWeekdays.map(day => <span key={day}>{day}</span>)}</div>
              {Array.from({ length: calendarLeadingDays }, (_, index) => <i className="calendar-blank" key={index} />)}
              {calendarDays.map(day => {
                const earnings = earningsByDay[day]?.earnings_gbp;
                const level = earnings !== null && earnings !== undefined && highestDailyEarnings > 0
                  ? Math.max(0, Math.min(1, Number(earnings) / highestDailyEarnings))
                  : 0;
                const cellStyle = level > 0
                  ? { backgroundColor: `hsl(91 42% ${98 - level * 16}%)`, borderColor: `hsl(91 34% ${90 - level * 22}%)` }
                  : undefined;
                return <article className={`calendar-day earnings-calendar-day ${day === todayDate ? "current-day" : ""} ${earnings === null || earnings === undefined ? "no-earnings" : ""}`} key={day} style={cellStyle}>
                  <time dateTime={day}>{Number(day.slice(8))}</time>
                  <strong data-mobile-value={calendarValue(earnings)}>{money(earnings)}</strong>
                </article>;
              })}
            </> : fiscalEarningMonths.map(month => {
              const amount = Number(month.total);
              const width = Number.isFinite(amount) && highestFiscalEarnings > 0
                ? Math.max(0, Math.min(100, amount / highestFiscalEarnings * 100))
                : 0;
              return <article className="earnings-year-total" key={`${month.year}-${month.month}`}>
                <time>{new Intl.DateTimeFormat("en-GB", { month: "short" }).format(new Date(`${month.year}-${month.month}-01T12:00:00Z`))}{month.incomplete ? "*" : ""}</time>
                <div className="earnings-year-bar" aria-hidden="true"><i style={{ width: `${width}%` }} /></div>
                <strong>{money(month.total)}</strong>
              </article>;
            })}
          </div>
          <Note className="finance-note">An asterisk after a month means some settled Octopus data has not arrived yet. Total earnings = savings + yield, using configured EcoFlow rates. Savings = EcoFlow to_home × configured import rate; yield = to_grid × configured export rate. Standing charges are excluded. Dynamic configured rates are averaged across each EcoFlow hourly interval.</Note>
        </section>

        <section className={activeTab !== "net-position" ? "page-heading tab-hidden" : "page-heading"}>
          <div><div className="eyebrow">HOME ENERGY / NET POSITION</div><h1>Costs and credit.<br />In one view.</h1><p>Octopus settlement costs, export credit, and gas costs.</p></div>
        </section>
        <section className={activeTab !== "net-position" ? "card finance-page tab-hidden" : "card finance-page"} aria-labelledby="net-position-page-heading">
          <div className="panel-heading"><div><h2 id="net-position-page-heading">Net position calendar</h2><p>Electricity and gas</p></div><div className="calendar-controls"><button type="button" className={calendarRange === "month" ? "active" : ""} onClick={() => setCalendarRange("month")}>Month</button><button type="button" className={calendarRange === "year" ? "active" : ""} onClick={() => setCalendarRange("year")}>Year</button>{calendarRange === "month" ? <label className="period-chip">Month <input aria-label="Net position calendar month" type="month" value={netCalendarDate.slice(0, 7)} onChange={(event) => setNetCalendarDate(event.target.value + "-01")} /></label> : <label className="period-chip">Year <input aria-label="Net position calendar year" type="number" min="2000" max="2100" value={calendarFiscalYear} onChange={(event) => changeCalendarYear(event.target.value, netCalendarDate, setNetCalendarDate)} /></label>}</div></div>
          <div className="net-position-glance">
            <article className={Number(selectedNetTotals.gas) >= 0 ? "net-glance-credit" : "net-glance-cost"}><div><span>{calendarRange === "month" ? "This month" : `Fiscal year (${fiscalYearLabel})`}</span><strong>{calendarLoading ? <span className="loading-spinner" /> : money(selectedNetTotals.gas)}</strong></div><p>Electricity only <b>{money(selectedNetTotals.electricity)}</b></p>{calendarRange === "month" && <div className="net-trend" aria-label="Daily net position trend">{calendarDays.map(day => { const value = Number(netPositionByDay[day]?.including_gas_gbp); const height = Number.isFinite(value) && highestDailyNetMagnitude > 0 ? Math.max(12, Math.abs(value) / highestDailyNetMagnitude * 100) : 12; return <i className={value < -0.05 ? "cost" : value > 0.05 ? "credit" : "neutral"} key={day} style={{height:`${height}%`}} />; })}</div>}</article>
            <article className={Number(calendarNetSummary?.lifetime?.including_gas_gbp) >= 0 ? "net-glance-credit" : "net-glance-cost"}><div><span>Lifetime</span><strong>{calendarLoading ? <span className="loading-spinner" /> : money(calendarNetSummary?.lifetime?.including_gas_gbp)}</strong></div><p>Electricity only <b>{money(calendarNetSummary?.lifetime?.electricity_only_gbp)}</b></p></article>
          </div>
          <div className="net-position-legend"><i className="net-credit-dot" /> Credit <i className="net-neutral-dot" /> Near zero <i className="net-cost-dot" /> Cost <span>Overall position; electricity-only is shown beneath.</span></div>
          <div className={calendarRange === "month" ? "calendar-grid net-month-grid" : "net-year-grid"} aria-label="Net position calendar">{calendarLoading && <span className="calendar-loading" aria-label="Loading net position calendar"><span className="loading-spinner" /></span>}
            {calendarRange === "month" ? <><div className="calendar-weekdays">{calendarWeekdays.map(day => <span key={day}>{day}</span>)}</div>{Array.from({length: calendarLeadingDays}, (_, index) => <i className="calendar-blank" key={index} />)}{calendarDays.map(day => { const value = netPositionByDay[day]; const gas = Number(value?.including_gas_gbp); const hasValue = value?.including_gas_gbp !== null && value?.including_gas_gbp !== undefined && Number.isFinite(gas); const state = !hasValue ? "net-no-data" : gas > 0.05 ? "net-credit" : gas < -0.05 ? "net-cost" : "net-neutral"; const fallback = Boolean(value?.ecoflow_fallback); return <button type="button" className={`calendar-day net-calendar-day ${state}`} key={day} onClick={() => openCalendarDetail(day)}><time dateTime={day}>{Number(day.slice(8))}</time><strong className={fallback ? "ecoflow-fallback" : ""} data-mobile-value={calendarValue(value?.including_gas_gbp)}>{money(value?.including_gas_gbp)}{fallback && value?.including_gas_gbp !== null ? <sup>**</sup> : ""}</strong><small>Electricity only <b className={fallback ? "ecoflow-fallback" : ""}>{money(value?.electricity_only_gbp)}</b></small></button>; })}</> : fiscalNetMonths.map(month => { const gas = Number(month.gas); const width = Number.isFinite(gas) && highestNetMagnitude > 0 ? Math.max(0, Math.min(100, Math.abs(gas) / highestNetMagnitude * 100)) : 0; const state = !Number.isFinite(gas) ? "net-no-data" : gas > 0.05 ? "net-credit" : gas < -0.05 ? "net-cost" : "net-neutral"; return <article className={`net-year-total ${state}`} key={`${month.year}-${month.month}`}><time>{new Intl.DateTimeFormat("en-GB", {month:"short"}).format(new Date(`${month.year}-${month.month}-01T12:00:00Z`))}{month.incomplete ? "*" : ""}</time><div className="net-year-bar" aria-hidden="true"><i style={{width:`${width}%`}} /></div><div><strong className={month.fallback ? "ecoflow-fallback" : ""}>{money(month.gas)}{month.fallback && month.gas !== null ? <sup>**</sup> : ""}</strong><small>Elec. {money(month.electricity)}</small></div></article>; })}
          </div>
          <Note className="finance-note">An asterisk after a month means some settled Octopus data has not arrived yet. Electricity only = settled export credit − settled import cost − electricity standing charge. The overall position also deducts settled gas cost and gas standing charge. Uses the same prices as Energy bills; missing readings or rates show —.</Note>
        </section>

        <section className={activeTab !== "bills" ? "page-heading tab-hidden" : "page-heading"}>
          <div><div className="eyebrow">HOME ENERGY / ENERGY BILLS</div><h1>Consumption costs.<br />In detail.</h1><p>Compare Octopus settlement consumption with EcoFlow house-use pricing.</p></div>
        </section>
        <section className={activeTab !== "bills" ? "card finance-page tab-hidden" : "card finance-page"} aria-labelledby="energy-bills-page-heading">
          <div className="panel-heading"><div><h2 id="energy-bills-page-heading">Energy bills</h2><p>Octopus consumption and EcoFlow house consumption</p></div><div className="calendar-controls"><label className="period-chip">Year <input aria-label="Energy bills year" type="number" min="2000" max="2100" value={billsDate.slice(0, 4)} onChange={(event) => changeCalendarYear(event.target.value, billsDate, setBillsDate)} /></label></div></div>
          <section className="bill-overview" aria-label="Year at a glance">
            <div><span>Grid import</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : money(moneySum(billTotal("electricity_actual"), billTotal("electricity_standing")))}</strong></div>
            <div><span>Gas</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : money(moneySum(billTotal("gas_actual"), billTotal("gas_standing")))}</strong></div>
            <div><span>Export credit</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : money(billTotal("electricity_export_actual"))}</strong></div>
            <div><span>EcoFlow house value</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : money(billTotal("ecoflow_actual"))}</strong></div>
            <div className="bill-overview-net"><span>Overall net position</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : money(totalIfPresent(energyBills.map(row => row.including_gas_gbp)))}</strong></div>
          </section>
          <div className="bill-month-grid" aria-label="Yearly energy bills">{energyBillsLoading && <span className="calendar-loading" aria-label="Loading energy bills"><span className="loading-spinner" /></span>}{calendarMonths.map(month => { const row = energyBills.find(item => item.month.slice(5, 7) === month); const net = Number(row?.including_gas_gbp); const netClass = Number.isFinite(net) ? net >= 0 ? "bill-credit" : "bill-cost" : "bill-empty"; return <article className={`bill-month ${netClass}`} key={month}><header><time>{new Intl.DateTimeFormat("en-GB", {month:"short"}).format(new Date(billsDate.slice(0, 4) + "-" + month + "-01T12:00:00Z"))}{row?.octopus_incomplete ? "*" : ""}</time><div><strong>{money(row?.including_gas_gbp)}</strong><span>Net position</span></div></header><dl><div><dt>Import + standing</dt><dd>{kwh(row?.electricity_kwh)} <b>{money(moneySum(row?.electricity_actual, row?.electricity_standing))}</b></dd></div><div><dt>Export credit</dt><dd>{kwh(row?.electricity_export_kwh)} <b>{money(row?.electricity_export_actual)}</b></dd></div><div><dt>Gas</dt><dd>{kwh(row?.gas_kwh)} <b>{money(moneySum(row?.gas_actual, row?.gas_standing))}</b></dd></div></dl><details><summary>EcoFlow comparison</summary><div><span>House electricity</span><b>{kwh(row?.ecoflow_kwh)} · {money(row?.ecoflow_actual)}</b></div><div><span>Electricity only</span><b>{money(row?.electricity_only_gbp)}</b></div></details></article>; })}</div>
          <Note className="finance-note">An asterisk after a month means some settled Octopus data has not arrived yet. Octopus amounts use settled meter readings × the Octopus rate in effect. EcoFlow house value = to_home × Octopus import rate, time-weighted when an interval spans rate changes. Missing prices show —; they are never treated as zero.</Note>
        </section>
        <section className={activeTab !== "bills" ? "card agreement-panel tab-hidden" : "card agreement-panel"} aria-labelledby="agreement-heading">
          <div className="panel-heading"><div><h2 id="agreement-heading">Current Octopus agreements</h2><p>Tariff codes from the latest imported Octopus rates</p></div></div>
          <div className="agreement-grid">
            {[['electricity_import', 'Electricity import'], ['electricity_export', 'Electricity export'], ['gas', 'Gas']].map(([key, label]) => { const meterPoint = key === 'electricity_import' ? overview?.identifiers?.ecoflow_octopus_import_mpan : key === 'electricity_export' ? overview?.identifiers?.ecoflow_octopus_export_mpan : overview?.identifiers?.ecoflow_octopus_gas_mprn; const agreement = tariffAgreements[key]; return <article key={key}><span>{meterPoint ? `${label} (${meterPoint})` : label}</span><strong>{energyBillsLoading ? <span className="loading-spinner" /> : agreement?.tariff_code ?? '—'}</strong><dl><div><dt>Tariff start</dt><dd>{agreementDate(agreement?.tariff_start)}</dd></div><div><dt>Tariff end</dt><dd>{agreementDate(agreement?.tariff_end)}</dd></div><div><dt>Standing charge</dt><dd>{pence(agreement?.standing_charge_pence)}{agreement?.standing_charge_pence !== null && agreement?.standing_charge_pence !== undefined ? ' / day' : ''}</dd></div><div><dt>{agreement?.variable ? 'Current day ave.' : 'Current rate'}</dt><dd>{pence(agreement?.current_day_average_pence)} / kWh</dd></div>{agreement?.variable && <div><dt>Historical average</dt><dd>{pence(agreement?.historical_average_pence)} / kWh</dd></div>}</dl></article>; })}
          </div>
          <Note className="agreement-note">Tariff agreements refresh with the selected billing year.</Note>
        </section>
        <section className={activeTab !== "history" ? "page-heading tab-hidden" : "page-heading"}>
          <div><div className="eyebrow">HOME ENERGY / HISTORY</div><h1>Energy use.<br />Over time.</h1><p>Compare solar, home, grid, battery, and gas energy for the selected period.</p></div>
        </section>

        <section className={`page-heading ${activeTab !== "overview" ? "tab-hidden" : ""}`}>
          <div>
            <div className="eyebrow">HOME ENERGY / OVERVIEW</div>
            <h1>
              A little more insight.
              <br className="mobile-break" /> A better balance.
            </h1>
            <p>Your solar, battery and grid. Together in one place.</p>
          </div>
          <div className="date-label">
            <Sun size={18} />
            <span>
              {day}
              <small>Europe / London</small>
            </span>
          </div>
        </section>

        <section id="power" className={activeTab !== "overview" ? "tab-hidden" : ""} aria-labelledby="power-heading">
          <div className="section-heading">
            <div>
              <h2 id="power-heading">
                Your home, right now{" "}
              </h2>
              <p>Instantaneous power · EcoFlow</p>
            </div>
            <span className="muted-state"><span className={`empty-dot ${ecoflow?.state === "healthy" ? "live" : ""}`} />{ecoflow?.state === "healthy" ? "Live telemetry" : ecoflow?.state === "syncing" ? "Connecting" : "Awaiting connection"}</span>
          </div>
          <div className="metrics-grid">
            <Metric
              title="Solar generation"
              value={reading("ecoflow_powerocean:solar_power", "W")}
              connected={liveConnected("ecoflow_powerocean:solar_power")}
              updating={livePowerUpdating || overviewLoading}
              description="Energy from your roof"
              icon={Sun}
              tone="solar"
            />
            <Metric
              title="Home consumption"
              value={reading("ecoflow_powerocean:home_power", "W")}
              connected={liveConnected("ecoflow_powerocean:home_power")}
              updating={livePowerUpdating || overviewLoading}
              description="Power used by your home"
              icon={House}
              tone="home"
            />
            <article className="metric card">
              <div className="metric-top"><span className="icon-tile grid"><PlugZap size={21} strokeWidth={1.7} /></span><span className="metric-inline-status">{livePowerUpdating || overviewLoading ? <span className="loading-spinner" aria-label="Refreshing live power" /> : <><span className={`empty-dot ${liveConnected("ecoflow_powerocean:grid_power") ? "live" : ""}`} />{liveConnected("ecoflow_powerocean:grid_power") ? "Live telemetry" : "Not connected"}</>}</span></div>
              <h3>Grid exchange</h3>
              <div className="metric-value flow-value" aria-label={gridFlow ? `${gridFlow.label} ${gridFlow.value} kW` : "Grid exchange unavailable"}>
                {gridFlow && <gridFlow.Icon size={20} strokeWidth={2} aria-hidden="true" />}
                <span>{gridFlow?.value ?? "—"}</span><span className="unit">kW</span>
              </div>
              <Note className="metric-note">Live grid flow by direction</Note>
            </article>
            <article className="metric card"><div className="metric-top"><span className="icon-tile battery"><Battery size={21} strokeWidth={1.7}/></span><span className="metric-inline-status">{livePowerUpdating || overviewLoading ? <span className="loading-spinner" aria-label="Refreshing live power" /> : <><span className={`empty-dot ${liveConnected("ecoflow_powerocean:battery_power") ? "live" : ""}`} />{liveConnected("ecoflow_powerocean:battery_power") ? "Live telemetry" : "Not connected"}</>}</span></div><h3>Battery power</h3><div className="metric-value flow-value" aria-label={batteryFlow ? `${batteryFlow.label} ${batteryFlow.value} kW` : "Battery power unavailable"}>{batteryFlow && <batteryFlow.Icon size={20} strokeWidth={2} aria-hidden="true" />}<span>{batteryFlow?.value ?? "—"}</span><span className="unit">kW</span></div><Note className="metric-note">Live battery flow by direction</Note></article>
          </div>
        </section>

        <section className={`daily-summary card ${activeTab !== "overview" ? "tab-hidden" : ""}`} aria-labelledby="daily-summary-heading">
          <div className="panel-heading"><div><h2 id="daily-summary-heading">Today energy</h2><p>EcoFlow current-day summary</p></div><span className="subtle-tag live-day-tag" aria-live="polite">Live day{summaryUpdating && <span className="live-day-spinner-overlay" aria-label="Updating live-day values"><span className="loading-spinner" /></span>}</span></div>
          <div className="daily-outcome-grid">
            <div className="daily-outcome daily-earnings"><Coins size={20}/><span>Total earnings</span><strong>{summaryLoading ? <span className="inline-loading" aria-label="Loading total earnings"><span className="loading-spinner" /></span> : money(todayOverview?.total_earnings_gbp)}</strong><small>Solar savings + export yield</small></div>
            <div className="daily-outcome independence"><Leaf size={22}/><span>Energy independence</span><strong>{summaryLoading ? <span className="inline-loading" aria-label="Loading energy independence"><span className="loading-spinner" /></span> : <>{Number.isFinite(energyIndependence) ? energyIndependence.toFixed(2) : "—"}<small>%</small></>}</strong><small>Self-powered today</small></div>
            <div className="daily-outcome daily-home"><House size={20}/><span>Home supplied</span><strong>{summaryLoading ? <span className="inline-loading" aria-label="Loading home energy"><span className="loading-spinner" /></span> : <>{summaryReading("ecoflow_to_home")} <small>kWh</small></>}</strong><small>Total energy to your home</small></div>
          </div>
          <section className="daily-flow" aria-labelledby="daily-flow-heading">
            <div className="daily-flow-heading"><div><h3 id="daily-flow-heading">Energy flow today</h3><p>Where your energy came from and where it went.</p></div></div>
            <div className="daily-flow-grid">
              <div className="daily-flow-column"><span className="daily-flow-label">Sources</span>{[['Solar','solar_generation_energy',Sun],['Battery','battery_discharge_energy',Battery],['Grid','grid_import_energy',ArrowDownLeft]].map(([label, metric, Icon]) => <div className={`daily-flow-item ${metric as string}`} key={metric as string}><Icon size={18}/><span>{label as string}</span><strong>{summaryLoading ? <span className="inline-loading"><span className="loading-spinner" /></span> : <>{summaryReading(metric as string)} <small>kWh</small></>}</strong></div>)}</div>
              <ChevronRight className="daily-flow-arrow" size={24} aria-hidden="true" />
              <div className="daily-flow-column"><span className="daily-flow-label">Where it went</span>{[['Home','ecoflow_to_home',House],['Battery','battery_charge_energy',BatteryCharging],['Grid','grid_export_energy',ArrowUpRight],['Power Pulse','ecoflow_to_power_pulse',CarFront]].filter(([, metric]) => metric !== 'ecoflow_to_power_pulse' || summaryLoading || energyNumber(metric as string) > 0).map(([label, metric, Icon]) => <div className={`daily-flow-item ${metric as string}`} key={metric as string}><Icon size={18}/><span>{label as string}</span><strong>{summaryLoading ? <span className="inline-loading"><span className="loading-spinner" /></span> : <>{summaryReading(metric as string)} <small>kWh</small></>}</strong></div>)}</div>
            </div>
          </section>
          <section className="daily-period-summary" aria-label="EcoFlow generation and earnings totals">
            <div className="daily-period-heading"><span>Performance</span><div role="tablist" aria-label="Performance period">{([['month', 'Month'], ['year', 'Fiscal year'], ['lifetime', 'Lifetime']] as const).map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={dailySummaryPeriod === key} onClick={() => setDailySummaryPeriod(key)}>{label}</button>)}</div></div>
            <div className="daily-period-values"><div><span>{dailyPeriod.label}</span><strong>{summaryLoading ? <span className="inline-loading"><span className="loading-spinner" /></span> : <>{periodReading(dailyPeriod.kwh)} <small>kWh</small></>}</strong><small>Generated</small></div><div><span>Generated value</span><strong>{summaryLoading ? <span className="inline-loading"><span className="loading-spinner" /></span> : money(dailyPeriod.value)}</strong></div><div><span>Total earnings</span><strong>{summaryLoading ? <span className="inline-loading"><span className="loading-spinner" /></span> : money(dailyPeriod.earnings)}</strong></div></div>
          </section>
          <Note className="generation-note"><span>Generated value uses the Octopus export rate at generation time × generated kWh.</span><span>Total earnings is solar savings (EcoFlow to_home × configured import rate) plus export yield (to_grid × configured export rate). It can exceed generated value when the configured import rate is higher than the export rate. Dynamic configured rates are averaged across EcoFlow hourly intervals.</span></Note>
        </section>

        <section className={`card net-position-panel ${activeTab !== "overview" ? "tab-hidden" : ""}`} aria-labelledby="net-position-heading">
          <div className="panel-heading"><div><h2 id="net-position-heading">Net position</h2><p>Octopus settlement costs and export credit, including standing charges</p></div><div className="history-actions"><label className="period-chip">Date <input aria-label="Net position date" type="date" value={selectedDate} onChange={(event) => handleSelectedDateChange(event.target.value)} /></label>{overviewLoading && <span className="subtle-tag net-position-status" aria-live="polite"><span className="loading-spinner" aria-label="Updating net position" /></span>}</div></div>
          <div className="net-position-tabs" role="tablist" aria-label="Net position period">{netPositionPeriods.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={netPositionPeriod === key} onClick={() => setNetPositionPeriod(key)}>{label}</button>)}</div>
          <section className="net-position-focus" aria-label={`${selectedNetPositionLabel} net position`}>
            <div className="net-position-focus-heading"><span>{selectedNetPositionLabel}</span><small>{netPositionPeriod === "day" ? "Selected date" : "Available settled data"}</small></div>
            <div className="net-position-focus-values"><div><span>Electricity only</span><strong className={selectedNetPositionFallback ? "ecoflow-fallback" : ""}>{money(selectedNetPosition?.electricity_only_gbp)}{selectedNetPositionFallback && selectedNetPosition?.electricity_only_gbp !== null ? <sup>**</sup> : ""}</strong><small>Export credit less import and standing charge</small></div><div><span>Overall position</span><strong className={selectedNetPositionFallback ? "ecoflow-fallback" : ""}>{money(selectedNetPosition?.including_gas_gbp)}{selectedNetPositionFallback && selectedNetPosition?.including_gas_gbp !== null ? <sup>**</sup> : ""}</strong><small>Electricity position less gas and gas standing charge</small></div></div>
          </section>
          <details className="net-position-explanation"><summary>How this is calculated</summary><p>Day uses EcoFlow export less import, valued at Octopus rates, less electricity standing charge; daily gas is unavailable. Longer periods use available settled Octopus costs and standing charges from Energy bills. Totals use available priced data; periods without priced data show —.</p></details>
        </section>


        {(hasSavedProjections || projectionLoading || todayOverview?.projection) && <section className={`card projection-panel ${activeTab !== "overview" ? "tab-hidden" : ""}`} aria-labelledby="projection-heading">
          {todayOverview?.projection ? <>
          <div className="panel-heading"><div><h2 id="projection-heading">Annual projection tracker</h2><p>Year {todayOverview.projection.year} · {todayOverview.projection.label}</p></div><span className={`subtle-tag ${todayOverview.projection.on_track ? "on-track" : "behind"}`}>{todayOverview.projection.on_track ? "On track" : "Below pace"}</span></div>
          <div className="projection-summary-cards">
            <section className="projection-summary-card" aria-label="Annual generation progress">
              <div className="projection-summary-heading"><span>Generation</span><strong>{Number(todayOverview.projection.actual_kwh).toFixed(3)} <small>kWh</small></strong></div>
              <p>of {Number(todayOverview.projection.target_kwh).toFixed(0)} kWh annual target</p>
              <div className="projection-progress-track" aria-label="Generation target progress"><i style={{width: `${todayOverview.projection.progress_percent ?? 0}%`}} /></div>
              <div className="projection-summary-forecast"><span>Forecast at current pace</span><strong>{Number(todayOverview.projection.forecast_kwh).toFixed(0)} <small>kWh</small></strong></div>
            </section>
            <section className="projection-summary-card" aria-label="Annual financial benefit progress">
              <div className="projection-summary-heading"><span>Financial benefit</span><strong>{money(todayOverview.projection.actual_benefit_gbp)}</strong></div>
              <p>of {money(todayOverview.projection.target_benefit_gbp)} annual target</p>
              <div className="projection-progress-track" aria-label="Annual benefit progress"><i style={{width: `${todayOverview.projection.benefit_progress_percent ?? 0}%`}} /></div>
              <div className="projection-summary-forecast"><span>Remaining to target</span><strong>{money(String(Math.max(0, Number(todayOverview.projection.target_benefit_gbp ?? 0) - Number(todayOverview.projection.actual_benefit_gbp ?? 0))))}</strong></div>
            </section>
          </div>
          {todayOverview.projection.target_system_net_position_gbp !== null && <section className="projection-system-summary" aria-label="System cost recovery"><div className="projection-system-summary-heading"><span>System position</span><small>Cost recovery</small></div><div className="projection-system-summary-values"><div><span>System cost</span><strong>{money(todayOverview.projection.target_system_cost_gbp)}</strong></div><div><span>Projected net position</span><strong>{money(todayOverview.projection.target_system_net_position_gbp)}</strong></div><div><span>Actual net position</span><strong>{money(todayOverview.projection.actual_system_net_position_gbp)}</strong></div></div><div className="projection-progress-track" aria-label="System cost recovery progress"><i style={{width:`${todayOverview.projection.system_position_progress_percent ?? 0}%`}} /></div></section>}
          <section className="projection-breakdown" aria-label="Projected and actual fiscal-year savings">
            <div className="projection-breakdown-heading"><span>Benefit breakdown</span><small>Actual compared with the annual projection</small></div>
            <div className="projection-breakdown-grid">
            {[['Solar + Battery savings', 'solar_battery_gbp', Sun], ['Export credit', 'export_gbp', ArrowUpRight]].map(([label, key, Icon]) => {
              const savingsKey = key as "solar_battery_gbp" | "export_gbp";
              const target = savingsKey === "solar_battery_gbp"
                ? Number(todayOverview.projection!.target_savings.solar_gbp) + Number(todayOverview.projection!.target_savings.battery_gbp)
                : Number(todayOverview.projection!.target_savings.export_gbp);
              const actual = savingsKey === "solar_battery_gbp"
                ? (todayOverview.projection!.actual_savings.solar_gbp == null ? NaN : Number(todayOverview.projection!.actual_savings.solar_gbp))
                : (todayOverview.projection!.actual_savings.export_gbp == null ? NaN : Number(todayOverview.projection!.actual_savings.export_gbp));
              return <div className="projection-breakdown-item" key={key as string}>
                <span className="projection-measure-title">{savingsKey === "solar_battery_gbp" ? <><Sun size={16} />Solar + <Battery className="battery-savings-icon" size={16} />Battery savings</> : <><Icon size={16} />{label as string}</>}</span><div className="projection-pair"><div><small>Projection</small><strong>{money(Number.isFinite(target) ? String(target) : null)}</strong></div><div><small>Actual{savingsKey === "export_gbp" ? "**" : "*"}</small><strong>{money(Number.isFinite(actual) ? String(actual) : null)}</strong></div></div>
              </div>;
            })}
            </div>
          </section>
          <Note className="projection-note"><span>*Solar + Battery actual = EcoFlow to_home × Octopus import rate at the time of use. Its projection is the configured solar projection plus battery projection.</span><span>**Export actual = settled Octopus export × Octopus export rate. Actuals sum available priced data. Longer energy intervals are time-weighted across rate changes.</span></Note>
          </> : projectionLoading ? <>
            <div className="panel-heading"><div><h2 id="projection-heading">Annual projection tracker</h2><p>Loading this fiscal year's projection</p></div><span className="subtle-tag" role="status" aria-label="Loading projection"><span className="loading-spinner" /></span></div>
            <div className="projection-grid projection-loading-tiles" role="status" aria-label="Loading annual projection">
              {['Actual generated', 'Annual generated target', 'Forecast at current pace', 'Annual benefit target', 'Actual benefit'].map((label) => <div key={label}><span>{label}</span><strong>—</strong></div>)}
            </div>
            <div className="projection-system-position projection-system-position-loading" aria-label="Loading system net position">
              {['System cost', 'Projected system net position', 'Actual system net position'].map((label) => <div key={label} className={label === 'Actual system net position' ? 'actual-system-position' : ''}><span>{label}</span><strong>—</strong></div>)}
            </div>
            <div className="projection-measures" aria-label="Loading projected and actual fiscal-year savings">
              {['Solar + Battery savings', 'Export credit'].map((label) => <div key={label}><span className="projection-measure-title">{label}</span><div className="projection-pair"><div><small>Projection</small><strong>—</strong></div><div><small>Actual</small><strong>—</strong></div></div></div>)}
            </div>
          </> : <>
            <div className="panel-heading"><div><h2 id="projection-heading">Annual projection tracker</h2><p>No annual target applies to the current fiscal year.</p></div></div>
          </>}
        </section>}

        <section className={activeTab !== "system" ? "system-live-strip tab-hidden" : "system-live-strip"} aria-label="Live system status">
          <div><span>Inverter</span><strong>{liveStatus("ecoflow_powerocean:ecoflow_inverter_status")}</strong></div>
          <div><span>Grid</span><strong>{liveStatus("ecoflow_powerocean:ecoflow_grid_connection")}</strong></div>
          <div><span>Last update</span><strong>{live["ecoflow_powerocean:battery_soc"]?.sampled_at ? new Date(live["ecoflow_powerocean:battery_soc"].sampled_at).toLocaleTimeString("en-GB") : "—"}</strong></div>
        </section>
        <div className={activeTab !== "system" ? "system-overview-grid tab-hidden" : "system-overview-grid"}>
          <section className="battery-panel" aria-labelledby="battery-heading">
            <div className="panel-heading">
              <h2 id="battery-heading">
                <Battery size={19} />
                Your battery
              </h2>
            </div>
            <div className="battery-body">
              <div>
                <span className="battery-percentage">
                  {reading("ecoflow_powerocean:battery_soc", "%")}<small>%</small>
                </span>
                <p>State of charge</p>
              </div>
              <div className={`battery-outline ${batteryIsFull ? "is-full" : ""}`} aria-hidden="true" style={{"--soc": `${Math.max(0, Math.min(100, Number.isFinite(soc) ? soc : 0))}%`} as React.CSSProperties}>
                <span><i />{batteryWatts > 0 ? <Zap className="battery-flow-icon charging" size={24} /> : batteryWatts < 0 ? <ArrowUpRight className="battery-flow-icon discharging" size={24} /> : null}</span>
                <div className="battery-scale"><small>0%</small><small>100%</small></div>
              </div>
            </div>
            <div className="battery-footer">
              <span>
                <span className="empty-dot" />
                {liveConnected("ecoflow_powerocean:battery_soc") ? "Live telemetry" : "Not connected"}
              </span>
            </div>
            {displayedPacks.length > 0 ? <div className="battery-pack-grid" aria-label="Attached battery details">{displayedPacks.map((pack, index) => <article key={pack.serial}><span>Pack {index + 1}</span><strong>{pack.serial}</strong><dl><div><dt>SOC</dt><dd>{pack.soc}%</dd></div><div><dt>SOH</dt><dd>{pack.soh}%</dd></div><div><dt>Capacity</dt><dd>{pack.capacity}</dd></div><div><dt>Cycles</dt><dd>{pack.cycles}</dd></div><div><dt>Temp.</dt><dd>{pack.temperature}°C</dd></div></dl></article>)}</div> : liveConnected("ecoflow_powerocean:battery_soc") && <p className="pack-details-unavailable">Waiting for battery serial and per-pack telemetry.</p>}
          </section>
          <section
            className="card system-panel"
            aria-labelledby="system-heading"
          >
            <div className="panel-heading"><h2 id="system-heading"><Activity size={19} />System details</h2></div>
            <details className="system-details" open><summary>Inverter and connection details</summary><dl>
              <div><dt>Inverter serial</dt><dd>{live["ecoflow_powerocean:ecoflow_inverter_serial"]?.value ?? "—"}</dd></div>
              <div><dt>Total battery capacity</dt><dd>{totalBatteryCapacity}</dd></div>
              <div><dt>Battery status</dt><dd>{liveConnected("ecoflow_powerocean:battery_soc") ? "Online" : "Unavailable"}</dd></div>
              <div><dt>Operating mode</dt><dd>{liveStatus("ecoflow_powerocean:ecoflow_operating_mode")}</dd></div>
            </dl></details>
          </section>
        </div>

        <section className={`card diagnostics-panel ${diagnostics.length ? "" : "diagnostics-healthy"} ${activeTab !== "system" ? "tab-hidden" : ""}`} aria-labelledby="diagnostics-heading">
          <div className="panel-heading"><div><h2 id="diagnostics-heading"><Activity size={19} /> Live diagnostics</h2><p>EcoFlow fault and warning codes observed from this point onward</p></div></div>
          {diagnostics.length ? <div className="diagnostics-list">{diagnostics.map((item: any) => <div key={`${item.label}-${item.value}`}><span className={`diagnostic-level ${item.severity.toLowerCase()}`}>{item.severity}</span><strong>{item.label}</strong><b>Code {item.value}</b><small>First seen {new Date(item.first_seen).toLocaleString("en-GB")} · Last seen {new Date(item.last_seen).toLocaleString("en-GB")}</small></div>)}</div> : <div className="diagnostics-empty"><ShieldCheck size={22} /><div><strong>No active faults or warnings</strong><span>New EcoFlow diagnostics will appear here when reported.</span></div></div>}
        </section>

        <section
          className={`card comparison ${activeTab !== "overview" ? "tab-hidden" : ""}`}
          aria-labelledby="comparison-heading"
        >
          <div className="panel-heading">
            <div>
              <h2 id="comparison-heading">Grid energy today</h2>
              <p>Settled meter readings alongside your EcoFlow estimate.</p>
            </div>
            <label className="period-chip">Date <input aria-label="Comparison date" type="date" value={selectedDate} onChange={(event) => handleSelectedDateChange(event.target.value)} /></label>
          </div>
          <div
            className="comparison-table"
            role="table"
            aria-label="Selected day grid energy comparison"
          >
            <div className="comparison-row comparison-head" role="row">
              <div role="columnheader">ENERGY</div>
              <div role="columnheader">
                <span className="provider-name octopus">
                  <Waves size={17} />
                  Settled meter
                </span>
                <small>Published after settlement</small>
              </div>
              <div role="columnheader">
                <span className="provider-name">
                  <Leaf size={17} />
                  EcoFlow estimate
                </span>
                <small>Current-day measurement</small>
              </div>
            </div>
            {[
              { label: "Grid import", icon: ArrowDownLeft },
              { label: "Grid export", icon: ArrowUpRight },
            ].map(({ label, icon: Icon }) => {
              const octopus = label === "Grid import" ? reading("octopus_import:grid_import_energy") : reading("octopus_export:grid_export_energy");
              const ecoflow = label === "Grid import" ? reading("ecoflow_powerocean:grid_import_energy") : reading("ecoflow_powerocean:grid_export_energy");
              return <div className="comparison-row" role="row" key={label}>
                <div role="rowheader">
                  <Icon size={17} />
                  {label}
                </div>
                <div role="cell">{overviewLoading ? <span className="inline-loading" aria-label="Loading Octopus reading"><span className="loading-spinner" /></span> : octopus === "—" ? <span className="comparison-pending">Pending settlement</span> : <>{octopus} <small>kWh</small></>}</div>
                <div role="cell">{overviewLoading ? <span className="inline-loading" aria-label="Loading EcoFlow reading"><span className="loading-spinner" /></span> : <>{ecoflow} <small>kWh</small></>}</div>
              </div>;
            })}
          </div>
          <Note>Octopus recorded import and export will take precedence when available. Missing readings are never shown as zero.</Note>
        </section>

        <div className={`lower-grid tab-${activeTab} ${activeTab === "imports" ? "tab-hidden" : ""}`}>
          <section className={`card system-panel gas-panel ${activeTab !== "overview" ? "tab-hidden" : ""}`} aria-label="Gas summary for selected date"><div className="panel-heading"><div><h2>Gas</h2><p>{selectedDateLabel}</p></div></div><div className="gas-status">{overviewLoading ? <span className="inline-loading gas-loading" aria-label="Loading gas reading"><span className="loading-spinner" /></span> : reading("octopus_gas:gas_consumption_kwh") === "—" ? <><strong>Pending settlement</strong><small>Octopus has not published this day’s gas reading.</small></> : <><strong>{reading("octopus_gas:gas_consumption_kwh")} <small>kWh</small></strong><small>Settled gas consumption</small></>}</div></section>
          <section className={`card system-panel mini-panel ${activeTab !== "overview" ? "tab-hidden" : ""}`} aria-labelledby="mini-heading">
            <div className="panel-heading"><div><h2 id="mini-heading"><Radio size={19} /> Octopus Mini live grid</h2><p>Live provider telemetry · not settlement data</p></div><span className="subtle-tag" aria-live="polite">{miniUpdating ? <span className="loading-spinner" aria-label="Refreshing Mini telemetry" /> : miniLive || mini?.state === "healthy" ? "Live" : "Unavailable"}</span></div>
            <div className="mini-groups"><section><h3>Meter totals</h3><dl>{[["Import meter", "octopus_mini_consumption_power", miniRegister("octopus_mini_consumption_power"), "kWh"], ["Export meter", "octopus_mini_export_power", miniRegister("octopus_mini_export_power"), "kWh"]].map(([label, key, value, unit]) => <div key={key as string}><dt>{label as string}</dt><dd>{value as string} <small>{unit as string}</small></dd></div>)}</dl></section><section><h3>Now</h3><dl>{[["Demand", "octopus_mini_demand_power", reading("octopus_mini:octopus_mini_demand_power", "W"), "kW"], ["This interval", "octopus_mini_consumption_delta", reading("octopus_mini:octopus_mini_consumption_delta"), "kWh"]].map(([label, key, value, unit]) => <div key={key as string}><dt>{label as string}</dt><dd>{value as string} <small>{unit as string}</small></dd></div>)}</dl></section></div>
            <Note>Mini readings are live telemetry; Octopus settlement records remain authoritative.</Note>
          </section>
          <section
            id="history"
            className={`card history-panel ${activeTab !== "history" ? "tab-hidden" : ""}`}
            aria-labelledby="history-heading"
          >
            <div className="panel-heading">
              <div>
                <h2 id="history-heading">History</h2>
                <p>Explore your energy flow and the rates behind it.</p>
                <div role="group" aria-label="History period">{["day", "week", "month"].map((value) => <button key={value} type="button" aria-pressed={period === value} onClick={() => setPeriod(value)}>{value}</button>)}</div>
              </div>
              <div className="history-actions"><label className="period-chip">Date <input aria-label="History date" type="date" value={historyDate} onChange={(event) => setHistoryDate(event.target.value)} /></label></div>
            </div>
            <div className="history-toolbar"><div role="group" aria-label="Energy view">{["Solar","Home","Grid","Battery","Gas"].map(value => <button key={value} type="button" aria-pressed={historyMode === value} onClick={() => setHistoryMode(value)}>{value}</button>)}</div><strong>{historyMode} flow · {historyDateLabel}</strong></div>
            <div className="history-costs" aria-label="Octopus cost and export-credit summary">{historyLoading ? ["import", "export", "gas", "net"].map((key) => <div className="loading-card" key={key}><span className="loading-spinner" /></div>) : <>
              <div><span>Net</span><strong className={historyNetFallback ? "ecoflow-fallback" : ""}>{money(historyNetPosition)}{historyNetFallback ? <sup>**</sup> : ""}</strong></div>
              <div><span>Import</span><strong>{money(historyCosts.import_cost_gbp)}</strong></div>
              <div><span>Export credit</span><strong className={historyExportFallback ? "ecoflow-fallback" : ""}>{money(historyCosts.export_credit_gbp)}{historyExportFallback && historyCosts.export_credit_gbp !== null ? <sup>**</sup> : ""}</strong></div>
              <div><span>Gas</span><strong>{money(historyCosts.gas_cost_gbp)}</strong></div>
            </>}
            </div>
            {historyLoading ? <div className="history-empty"><span className="loading-spinner" /><h3>Loading selected range</h3></div> : historyRows.length === 0 ? <div className="history-empty"><span className="empty-chart-icon"><Activity size={28} /></span><h3>No readings for this period</h3><p>Choose another date or wait for the next import.</p></div> : <EnergyChart rows={historyRows} mode={historyMode} identifiers={overview?.identifiers} />}
            <Note className="history-cost-note">Export credit and net position use settled Octopus readings when available. * uses EcoFlow to_grid valued at the Octopus export rate when settlement data has not arrived. Values remain — until the required readings, rates and standing charges arrive.</Note>
          </section>

          <section className={`card rates-panel ${activeTab !== "history" ? "tab-hidden" : ""}`} aria-labelledby="rates-heading">
            <div className="panel-heading"><div><h2 id="rates-heading">Rates for this day</h2><p>Unit rates and standing charges for {historyDateLabel}</p></div></div>
            <div className="rate-grid">{ratesLoading ? [{ code: "import", label: "Electricity import" }, { code: "export", label: "Electricity export" }, { code: "gas", label: "Gas" }].map((rate) => <RateLoadingCard key={rate.code} {...rate} mpan={rate.code === "import" ? overview?.identifiers?.ecoflow_octopus_import_mpan : rate.code === "export" ? overview?.identifiers?.ecoflow_octopus_export_mpan : overview?.identifiers?.ecoflow_octopus_gas_mprn} />) : octopusRates.map((rate) => <RateCard key={rate.code} rate={rate} mpan={rate.code === "import" ? overview?.identifiers?.ecoflow_octopus_import_mpan : rate.code === "export" ? overview?.identifiers?.ecoflow_octopus_export_mpan : overview?.identifiers?.ecoflow_octopus_gas_mprn} />)}</div>
            <Note className="rates-note">Rates use the selected date and the latest imported tariff history.</Note>
          </section>

          <section className={`card vpp-panel ${activeTab !== "vpp" ? "tab-hidden" : ""}`} aria-labelledby="vpp-heading">
            <div className="panel-heading"><div><h2 id="vpp-heading"><Zap size={20} />Axle VPP</h2><p>Upcoming events, live status, and recorded event export.</p></div><span className={`subtle-tag ${vpp?.event?.state === "in_progress" ? "live-day-tag" : ""}`}>{vpp?.event?.state === "in_progress" ? "Live event" : vpp?.configured ? "Connected" : "Not connected"}</span></div>
            {!vpp && !vppError && <div className="history-empty"><span className="loading-spinner" /><h3>Loading Axle events</h3></div>}
            {vppError && <p className="setup-error">{vppError}</p>}
            {vpp && !vpp.configured && <div className="vpp-empty"><Zap size={28} /><h3>Connect Axle to view VPP events</h3><p>Add your Axle API key in Settings. The dashboard securely fetches the current or next event and records it for this timeline.</p>{onSettings && <button type="button" className="secondary-button" onClick={onSettings}>Open settings</button>}</div>}
            {vpp?.configured && <>
              <div className="vpp-event-hero">{vpp.event ? <><div><span>{vpp.event.direction === "export" ? "Export event" : vpp.event.direction === "import" ? "Import event" : "Grid event"}</span><strong>{vpp.event.state === "in_progress" ? "In progress" : "Upcoming"}</strong></div><div><small>Window</small><b>{new Date(vpp.event.start_at).toLocaleString("en-GB", {weekday:"short", day:"numeric", month:"short", hour:"2-digit", minute:"2-digit"})} – {new Date(vpp.event.end_at).toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit"})}</b></div><div><small>Recorded export</small><b>{Number(vpp.event.settled_export_kwh).toFixed(3)} kWh</b></div></> : <><div><span>Axle status</span><strong>No scheduled event</strong></div><p>When Axle announces an import or export window, it will appear here and be kept in your event timeline.</p></>}</div>
              <div className="vpp-timeline-heading"><h3>Event timeline</h3><small>Settled export appears after Octopus publishes the meter interval.</small></div>
              <div className="vpp-event-list">{vpp.events.length ? vpp.events.map(event => <article key={`${event.start_at}-${event.end_at}`}><span className={`vpp-direction ${event.direction}`}>{event.direction}</span><div><strong>{new Date(event.start_at).toLocaleDateString("en-GB", {weekday:"short", day:"numeric", month:"short"})}</strong><small>{new Date(event.start_at).toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit"})} – {new Date(event.end_at).toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit"})}</small></div><div><small>Settled export</small><b>{Number(event.settled_export_kwh).toFixed(3)} kWh</b></div><em>{event.state === "in_progress" ? "Live" : event.state === "upcoming" ? "Scheduled" : "Completed"}</em></article>) : <p className="vpp-no-events">No Axle events have been announced yet.</p>}</div>
            </>}
          </section>

          <section
            id="connections"
            className={`card connections-panel ${activeTab !== "connections" ? "tab-hidden" : ""}`}
            aria-labelledby="connections-heading"
          >
            <div className="panel-heading">
              <div>
                <h2 id="connections-heading">Connections</h2>
                <p>Your dashboard's data sources</p>
              </div>
              <Cable size={20} className="muted-icon" />
            </div>
            <ConnectionRow
              icon={Leaf}
              title="EcoFlow"
              detail={ecoflow ? `MQTT: ${ecoflow.mqtt_connection ?? "unknown"} · Data: ${ecoflow.receiving_data ? "receiving" : "not receiving"}. ${ecoflow.reason}` : "PowerOcean · live & history"}
              label={providerLabel(ecoflow)}
              help={importHelp(ecoflow, "EcoFlow")}
              good={ecoflow?.state === "healthy"}
            />
            <ConnectionRow
              icon={Waves}
              title="Octopus Energy"
              detail={octopus?.reason ?? "Recorded import & export"}
              label={octopusLabel}
              help={importHelp(octopus, "Octopus Energy")}
              good={octopus?.state === "healthy"}
            />
            <ConnectionRow
              icon={Radio}
              title="Octopus Mini"
              detail={mini?.reason ?? "Live grid measurements"}
              label={providerLabel(mini)}
              help={mini?.state === "healthy" ? undefined : mini?.state === "failed" ? `${mini.reason ?? "Octopus Mini discovery did not return live telemetry."} The Mini is optional and is discovered automatically from your Octopus account. If you use one, inspect: docker compose -f production-compose.yaml logs --tail=100 collector` : importHelp(mini, "Octopus Mini")}
              good={mini?.state === "healthy"}
            />
            <ConnectionRow
              icon={Waves}
              title="Octopus Gas"
              detail={gas?.reason ?? "Recorded gas consumption & tariffs"}
              label={providerLabel(gas)}
              help={importHelp(gas, "Octopus Gas")}
              good={gas?.state === "healthy"}
            />
            <div className="infrastructure">
              <span className="micro-label">WORKSPACE SERVICES</span>
              <ConnectionRow
                icon={Database}
                title="Database"
                detail="PostgreSQL"
                label={
                  databaseReady
                    ? "Ready"
                    : reachable
                      ? "Unavailable"
                      : "Unknown"
                }
                good={databaseReady}
              />
              <ConnectionRow
                icon={Activity}
                title="Collector"
                detail="Heartbeat, telemetry maintenance & provider imports"
                label={collectorLabel}
                good={collector === "healthy"}
              />
            </div>
            <Note>Connection status reflects the latest provider response and telemetry received by the dashboard.</Note>
          </section>
        </div>

        <section id="imports" className={`card operations-panel ${activeTab !== "imports" ? "tab-hidden" : ""}`} aria-labelledby="imports-heading">
          <div className="panel-heading"><div><h2 id="imports-heading">Import statistics</h2></div><div className="imports-actions"><button type="button" className="secondary-button" onClick={retryPartialImports} disabled={retryingPartials}>{retryingPartials ? "Queuing retry…" : "Retry partial imports now"}</button>{operationsLoading ? <span className="loading-spinner" aria-label="Loading import statistics" /> : <Database size={20} className="muted-icon" />}</div></div>
          <div className="operations-table" role="table" aria-label="Data import statistics"><div className="operations-row operations-header" role="row"><span>Source</span><span>Dataset</span><span>Coverage</span><span>Left to import</span><span>Next run</span><span>Last run</span><span>Received / persisted</span></div>{operations.imports.map(row => <div className="operations-row" role="row" key={`${row.source}-${row.dataset ?? "live"}`}><strong>{row.source}</strong><span>{row.dataset ?? "—"}</span><span>{row.coverage_start ? `${new Date(row.coverage_start).toLocaleDateString("en-GB")} – ${new Date(row.coverage_end).toLocaleDateString("en-GB")}` : "No coverage yet"}<small>{row.complete_windows} complete · {row.partial_windows} partial · {row.failed_windows} failed{row.missing_windows !== undefined ? ` · ${row.missing_windows} missing` : ""}</small></span><span>{row.percent_left_to_import === undefined ? "—" : `${row.percent_left_to_import}%`}<small>{row.expected_windows === undefined ? "Awaiting provider coverage" : `${row.remaining_windows ?? 0} of ${row.expected_windows} windows`}</small></span><span>{row.next_run_status ?? (row.next_run_at ? new Date(row.next_run_at).toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—")}<small>{row.next_run_at ? "Scheduled" : ""}</small></span><span>{row.status === "running" ? "Running" : row.status === "failed" ? "Failed" : row.status === "partial" ? "Partial" : row.status === "complete" ? "Complete" : "Waiting"}<small>{row.completed_at ? new Date(row.completed_at).toLocaleString("en-GB") : "—"}</small></span><span>{row.rows_received ?? 0} / {row.rows_written ?? 0}</span></div>)}</div>
          <Note className="operations-note">Coverage, last run, and persisted rows per source. Octopus settlement is delayed by 1–2 days; older gaps remain partial until supplied by the provider.</Note>
        </section>

        <section id="console" className={`card operations-panel ${activeTab !== "console" ? "tab-hidden" : ""}`} aria-labelledby="console-heading">
          <div className="panel-heading"><div><h2 id="console-heading">Collector console</h2><p>Persisted collector events and received telemetry.</p></div><span className="subtle-tag">Pending writes {operations.pending_persistence}</span></div>
          <div className="console-grid"><div><h3>Events</h3><div className="console-list">{operations.events.slice(0, 16).map((event, index) => <p key={`${event.source}-${event.started_at}-${index}`}><strong>{event.source}</strong> · {event.dataset} · {event.status} <small>{event.rows_received} received / {event.rows_written} persisted · {new Date(event.started_at).toLocaleString("en-GB")}{event.failure_detail ? ` · ${event.failure_detail}` : ""}</small></p>)}</div></div><div><h3>Received telemetry</h3><div className="console-list">{operations.received.slice(0, 16).map((item, index) => <p key={`${item.source}-${item.metric_code}-${item.received_at}-${index}`}><strong>{item.source}</strong> · {item.metric_code}: {item.value} <small>{new Date(item.received_at).toLocaleString("en-GB")} · {item.quality}</small></p>)}</div></div></div>
        </section>

        <section id="docker-logs" className={`card operations-panel logs-panel ${activeTab !== "docker-logs" ? "tab-hidden" : ""}`} aria-labelledby="docker-logs-heading">
          <div className="panel-heading"><div><h2 id="docker-logs-heading">Collector logs</h2><p>Live collector output. It is held only in memory and clears when the API restarts.</p></div><span className="subtle-tag">Refreshing</span></div>
          <pre className="collector-log-output" aria-live="polite">{collectorLogs.length ? [...collectorLogs].reverse().join("\n") : "Waiting for collector messages…"}</pre>
          <Note>Decoded EcoFlow payload summaries are redacted before logging. Credentials, tokens and device identifiers are excluded.</Note>
        </section>

        {calendarDetailDate && activeTab === "net-position" && <div className="calendar-modal-backdrop" role="presentation" onClick={() => setCalendarDetailDate(null)}><section className="calendar-modal" role="dialog" aria-modal="true" aria-label="Calendar day details" onClick={(event) => event.stopPropagation()}><button type="button" className="calendar-modal-close" aria-label="Close details" onClick={() => setCalendarDetailDate(null)}><X size={20} /></button><time>{new Intl.DateTimeFormat("en-GB", {weekday:"long", day:"numeric",month:"long", year:"numeric"}).format(new Date(calendarDetailDate + "T12:00:00Z"))}</time><div><span>Electricity only</span><strong>{money(netPositionByDay[calendarDetailDate]?.electricity_only_gbp)}</strong></div><div><span>Overall position</span><b>{money(netPositionByDay[calendarDetailDate]?.including_gas_gbp)}</b></div></section></div>}

        <footer>
          <div role="status" aria-live="polite" className="stream-status">
            <span className={`connection-dot ${reachable ? "online" : ""}`} />
            Status stream {connection}
            <span className="last-update">· {observedAt}</span>
          </div>
        </footer>
        <a href="#main" className="back-top">
          Back to top <ChevronRight size={14} />
        </a>
      </main>
    </>
  );
}
