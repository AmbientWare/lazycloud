"use client";

import { StepCard } from "./step-card";
import { StepLayout } from "./step-layout";
import { TerminalWindow } from "./terminal-window";
import { FeatureList } from "./feature-list";
import { ServiceListItem } from "./service-list-item";
import { StatusIndicator } from "../status-indicator";

interface StepLiveProps {
  id?: string;
  stackIndex?: number;
}

const FEATURES = [
  "Instant public URL with automatic HTTPS",
  "Built-in DDoS protection & firewall",
  "gVisor container isolation for security",
];

const SERVICES = [
  { name: "web", cpu: "0.08", mem: "128", replicas: 2 },
  { name: "api", cpu: "0.15", mem: "256", replicas: 1 },
  { name: "db", cpu: "0.12", mem: "512", replicas: 1 },
];

function StatusDashboard() {
  return (
    <TerminalWindow title="lazycloud status" className="h-[340px]">
      <div className="h-full overflow-hidden bg-card/95 p-4 font-mono text-xs">
        {/* Project header */}
        <div className="mb-4 flex items-center justify-between">
          <span className="text-sm font-semibold text-foreground">
            my-project
          </span>
          <StatusIndicator status="running" />
        </div>

        {/* Quick stats */}
        <div className="mb-4 space-y-1.5 text-muted-foreground">
          <div className="flex justify-between">
            <span>Services</span>
            <span className="text-foreground">3/3 ready</span>
          </div>
          <div className="flex justify-between">
            <span>Replicas</span>
            <span className="text-foreground">4/4 running</span>
          </div>
          <div className="flex justify-between">
            <span>Last deploy</span>
            <span className="text-foreground">2 min ago</span>
          </div>
        </div>

        {/* Service details */}
        <div className="border-t border-border/40 pt-3">
          <p className="mb-2 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
            Services
          </p>
          <div className="space-y-2">
            {SERVICES.map((svc) => (
              <ServiceListItem
                key={svc.name}
                name={svc.name}
                cpu={svc.cpu}
                mem={svc.mem}
                replicas={svc.replicas}
                variant="detailed"
              />
            ))}
          </div>
        </div>

        {/* URL footer */}
        <div className="mt-4 flex items-center justify-between border-t border-border/40 pt-3 text-[11px]">
          <span className="text-muted-foreground">URL</span>
          <span className="text-lazycloud">my-project.lazycloud.dev</span>
        </div>
      </div>
    </TerminalWindow>
  );
}

export function StepLive({ id, stackIndex = 0 }: StepLiveProps) {
  return (
    <StepCard id={id} stepNumber="03" stepLabel="Live" stackIndex={stackIndex}>
      <StepLayout
        title="Your app is live"
        description="Instantly accessible to the world with enterprise-grade security built in. No configuration required."
        features={<FeatureList items={FEATURES} />}
        visual={<StatusDashboard />}
      />
    </StepCard>
  );
}
