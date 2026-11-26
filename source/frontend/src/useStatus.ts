import { useEffect, useState } from "react";

export type SystemStatus = {
  observed_at: string;
  database: "ready" | "unavailable";
  collector: {
    state: "healthy" | "stale" | "waiting" | "unknown";
    last_heartbeat: string | null;
    age_seconds: number | null;
  };
  providers: Record<
    "ecoflow" | "octopus" | "octopus_gas" | "octopus_mini",
    {
      state: "not_connected" | "backfilling" | "syncing" | "partial" | "healthy" | "failed";
      reason: string;
      mqtt_connection?: "connecting" | "connected" | "stale" | "disconnected" | "unknown" | null;
      receiving_data?: boolean | null;
      last_sample_at?: string | null;
    }
  >;
};

export function useStatus() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [connection, setConnection] = useState<
    "connecting" | "connected" | "interrupted"
  >("connecting");

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const accept = (next: SystemStatus) => {
      if (!active) return;
      setStatus((previous) =>
        !previous || next.observed_at >= previous.observed_at ? next : previous,
      );
    };
    const refresh = async () => {
      try {
        const response = await fetch("/api/status", {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Status unavailable");
        accept(await response.json());
      } catch {
        // SSE is the connection authority; a single HTTP error must not override it.
      }
    };
    void refresh();
    let events: EventSource;
    let lastEventAt = Date.now();
    const connect = () => {
      lastEventAt = Date.now();
      events = new EventSource("/api/events");
      events.onopen = () => { void refresh(); };
      events.addEventListener("status", (event) => {
        try {
          accept(JSON.parse((event as MessageEvent).data));
          lastEventAt = Date.now();
          if (active) setConnection("connected");
        } catch {
          if (active) setConnection("interrupted");
        }
      });
      events.onerror = () => {
        if (active) setConnection("interrupted");
      };
    };
    connect();
    // A reverse proxy can leave the downstream socket open after its upstream
    // disappears. Detect silence too, rather than relying only on onerror.
    const watchdog = window.setInterval(() => {
      if (Date.now() - lastEventAt > 15_000) {
        setConnection("interrupted");
        events.close();
        connect();
      }
    }, 1000);
    return () => {
      active = false;
      controller.abort();
      window.clearInterval(watchdog);
      events.close();
    };
  }, []);

  return { status, connection };
}
