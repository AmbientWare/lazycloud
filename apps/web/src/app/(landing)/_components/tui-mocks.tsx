"use client";

import { cn } from "@/lib/utils";
import { StatusIndicator, CostIndicator } from "./status-indicator";
import { ServiceListItem } from "./how-it-works/service-list-item";

// Deployment status overview
export function DeploymentStatusCard() {
  return (
    <div className="font-mono text-xs">
      {/* Status header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">myapp</span>
        <StatusIndicator status="running" />
      </div>

      {/* Quick stats */}
      <div className="space-y-2">
        <div className="flex items-center justify-between text-muted-foreground">
          <span>Services</span>
          <span className="text-foreground">3/3 ready</span>
        </div>
        <div className="flex items-center justify-between text-muted-foreground">
          <span>Replicas</span>
          <span className="text-foreground">5/5 running</span>
        </div>
        <div className="flex items-center justify-between text-muted-foreground">
          <span>Last deploy</span>
          <span className="text-foreground">2 min ago</span>
        </div>
      </div>

      {/* Mini service list */}
      <div className="mt-4 space-y-1.5 border-t border-border/40 pt-3">
        {["web", "api", "worker"].map((name) => (
          <ServiceListItem key={name} name={name} variant="simple" />
        ))}
      </div>
    </div>
  );
}

// Service resource metrics
export function ServiceMetricsCard() {
  return (
    <div className="font-mono text-xs">
      {/* Service header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">api</span>
        <span className="text-muted-foreground">2 instances</span>
      </div>

      {/* Resource bars */}
      <div className="space-y-3">
        <div>
          <div className="mb-1 flex justify-between text-muted-foreground">
            <span>CPU</span>
            <span className="text-foreground">0.12 / 0.50 cores</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full w-[24%] rounded-full bg-lazycloud" />
          </div>
        </div>
        <div>
          <div className="mb-1 flex justify-between text-muted-foreground">
            <span>Memory</span>
            <span className="text-foreground">256 / 512 MB</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full w-[50%] rounded-full bg-lazycloud" />
          </div>
        </div>
      </div>

      {/* Instance status */}
      <div className="mt-4 space-y-1.5 border-t border-border/40 pt-3 text-[11px]">
        <div className="flex items-center justify-between">
          <span className="text-muted-foreground">api-x2k4m</span>
          <span className="text-green-500">Running</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-muted-foreground">api-h8n3p</span>
          <span className="text-green-500">Running</span>
        </div>
      </div>
    </div>
  );
}

// Secrets management
export function SecretsCard() {
  const secrets = [
    "DATABASE_URL",
    "API_SECRET_KEY",
    "STRIPE_KEY",
    "REDIS_URL",
  ];

  return (
    <div className="font-mono text-xs">
      {/* Header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">Secrets</span>
        <span className="text-muted-foreground">{secrets.length} configured</span>
      </div>

      {/* Secret list */}
      <div className="space-y-2">
        {secrets.map((name, idx) => (
          <div
            key={name}
            className={cn(
              "flex items-center justify-between rounded px-2 py-1.5",
              idx === 0 && "bg-lazycloud/10"
            )}
          >
            <div className="flex items-center gap-2">
              <span className="h-1.5 w-1.5 rounded-full bg-green-500" />
              <span className="text-foreground">{name}</span>
            </div>
            <span className="text-muted-foreground">••••••</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Usage and billing
export function UsageBillingCard() {
  const metrics = [
    { label: "CPU", usage: "12.4 core-hrs", cost: "$1.24" },
    { label: "Memory", usage: "48.2 GB-hrs", cost: "$0.96" },
    { label: "Storage", usage: "120 GB-hrs", cost: "$0.48" },
    { label: "Build", usage: "45 min", cost: "$0.90" },
  ];

  return (
    <div className="font-mono text-xs">
      {/* Header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">This Month</span>
        <CostIndicator value="$3.58" />
      </div>

      {/* Usage metrics */}
      <div className="space-y-2">
        {metrics.map((metric) => (
          <div key={metric.label} className="flex items-center justify-between">
            <span className="text-muted-foreground">{metric.label}</span>
            <div className="flex items-center gap-3">
              <span className="text-foreground">{metric.usage}</span>
              <span className="w-12 text-right text-lazycloud">{metric.cost}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Billing period */}
      <div className="mt-4 border-t border-border/40 pt-3 text-[11px]">
        <div className="flex items-center justify-between text-muted-foreground">
          <span>Billing period</span>
          <span className="text-foreground">Jan 1 - Jan 17</span>
        </div>
        <div className="mt-1 flex items-center justify-between text-muted-foreground">
          <span>Projected</span>
          <span className="text-foreground">~$6.50</span>
        </div>
      </div>
    </div>
  );
}

// Live logs streaming
export function LogsCard() {
  const logs = [
    { level: "INFO", color: "text-green-500", msg: "Server started on :8000" },
    { level: "INFO", color: "text-green-500", msg: "Connected to database" },
    { level: "WARN", color: "text-yellow-500", msg: "Cache miss: session_abc" },
    { level: "INFO", color: "text-green-500", msg: "GET /api/users 200 45ms" },
  ];

  return (
    <div className="font-mono text-xs">
      {/* Header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">Live Logs</span>
        <span className="text-muted-foreground">api-x2k4m</span>
      </div>

      {/* Log entries */}
      <div className="space-y-1.5 rounded border border-border/40 bg-muted/20 p-2">
        {logs.map((log, idx) => (
          <div key={idx} className="flex gap-2 text-[11px]">
            <span className={cn("w-10 shrink-0 font-semibold", log.color)}>
              {log.level}
            </span>
            <span className="truncate text-foreground">{log.msg}</span>
          </div>
        ))}
        {/* Blinking cursor */}
        <div className="flex gap-2 text-[11px]">
          <span className="w-10 shrink-0 font-semibold text-green-500">INFO</span>
          <span className="text-foreground">
            Waiting...
            <span className="ml-0.5 inline-block h-2.5 w-1 animate-pulse bg-lazycloud" />
          </span>
        </div>
      </div>
    </div>
  );
}

// Container management actions
export function ContainerManagementCard() {
  const actions = [
    { name: "Restart", key: "r", status: "ready" },
    { name: "Rollback", key: "b", status: "ready" },
    { name: "Delete", key: "d", status: "ready" },
  ];

  return (
    <div className="font-mono text-xs">
      {/* Header */}
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-foreground">Actions</span>
        <span className="text-muted-foreground">myapp</span>
      </div>

      {/* Action list */}
      <div className="space-y-2">
        {actions.map((action, idx) => (
          <div
            key={action.name}
            className={cn(
              "flex items-center justify-between rounded px-2 py-1.5",
              idx === 0 && "bg-lazycloud/10"
            )}
          >
            <div className="flex items-center gap-2">
              <span className="flex h-5 w-5 items-center justify-center rounded border border-border/60 bg-muted/40 text-[10px] font-semibold text-muted-foreground">
                {action.key}
              </span>
              <span className="text-foreground">{action.name}</span>
            </div>
            <span className="text-[10px] text-green-500">
              {action.status}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
