"use client";

import type { ServiceStatusSummary, ProbeConfig } from "@/interfaces/deployments";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ExternalLink, Copy, Check } from "lucide-react";
import { useState } from "react";

interface ServiceDetailsSheetProps {
  service: ServiceStatusSummary | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function getStatusColor(status: string) {
  switch (status.toLowerCase()) {
    case "running":
      return "bg-green-500/10 text-green-500 border-green-500/30";
    case "pending":
    case "creating":
    case "health_check":
    case "updating":
      return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
    case "error":
    case "failed":
    case "restarting":
      return "bg-red-500/10 text-red-500 border-red-500/30";
    case "exited":
    case "stopping":
      return "bg-gray-500/10 text-gray-500 border-gray-500/30";
    default:
      return "";
  }
}

function formatProbeType(probe: ProbeConfig | null | undefined): string {
  if (!probe) return "Not configured";
  if (probe.httpGet) {
    return `HTTP ${probe.httpGet.path}:${probe.httpGet.port}`;
  }
  if (probe.tcpSocket) {
    return `TCP port ${probe.tcpSocket.port}`;
  }
  if (probe.exec) {
    return "Exec command";
  }
  return "Configured";
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-sm font-semibold text-foreground border-b border-border/50 pb-2 mb-3">
      {children}
    </h3>
  );
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between items-center py-1.5">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm font-medium">{value}</span>
    </div>
  );
}

export function ServiceDetailsSheet({
  service,
  open,
  onOpenChange,
}: ServiceDetailsSheetProps) {
  const [copied, setCopied] = useState(false);

  if (!service) return null;

  const copyEndpoint = async () => {
    if (service.endpoint) {
      await navigator.clipboard.writeText(`https://${service.endpoint}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const hasResources = service.resources?.requests || service.resources?.limits || service.current_usage;
  const hasHealthChecks = service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe;
  const hasHPA = service.hpa?.enabled;
  const hasPods = service.pods && service.pods.length > 0;

  // Extract HPA metrics for display
  const getHPATargets = () => {
    if (!service.hpa?.metrics) return { cpu: null, memory: null };
    let cpu: number | null = null;
    let memory: number | null = null;
    for (const metric of service.hpa.metrics) {
      if (metric.type === "Resource" && metric.resource) {
        const name = metric.resource.name as string;
        const target = metric.resource.target as { averageUtilization?: number };
        if (name === "cpu" && target?.averageUtilization) {
          cpu = target.averageUtilization;
        }
        if (name === "memory" && target?.averageUtilization) {
          memory = target.averageUtilization;
        }
      }
    }
    return { cpu, memory };
  };

  const hpaTargets = getHPATargets();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-lg">
        <SheetHeader className="pb-4">
          <div className="flex items-center gap-3">
            <Badge
              variant="outline"
              className={`text-xs capitalize ${getStatusColor(service.status)}`}
            >
              {service.status}
            </Badge>
            <SheetTitle className="text-lg">{service.name}</SheetTitle>
          </div>
          <SheetDescription className="text-xs font-mono truncate">
            {service.image || "No image specified"}
          </SheetDescription>
        </SheetHeader>

        <ScrollArea className="h-[calc(100vh-120px)] pr-4">
          <div className="space-y-6">
            {/* Overview Section */}
            <div>
              <SectionHeader>Overview</SectionHeader>
              <div className="space-y-1">
                <InfoRow
                  label="Replicas"
                  value={`${service.ready_replicas}/${service.total_replicas} ready`}
                />
                {service.ports && service.ports.length > 0 && (
                  <InfoRow
                    label="Ports"
                    value={service.ports.join(", ")}
                  />
                )}
                {service.restarts > 0 && (
                  <InfoRow
                    label="Restarts"
                    value={<span className="text-destructive">{service.restarts}</span>}
                  />
                )}
              </div>
              {service.endpoint && (
                <div className="mt-3 flex items-center gap-2">
                  <a
                    href={`https://${service.endpoint}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1.5 text-sm text-cyan-500 hover:text-cyan-400 transition-colors truncate"
                  >
                    <ExternalLink className="h-3.5 w-3.5 shrink-0" />
                    <span className="truncate">{service.endpoint}</span>
                  </a>
                  <button
                    onClick={copyEndpoint}
                    className="text-muted-foreground hover:text-foreground transition-colors shrink-0"
                    title="Copy endpoint"
                  >
                    {copied ? (
                      <Check className="h-3.5 w-3.5 text-green-500" />
                    ) : (
                      <Copy className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
              )}
            </div>

            {/* Resources Section */}
            {hasResources && (
              <div>
                <SectionHeader>Resources</SectionHeader>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[80px]">Resource</TableHead>
                      <TableHead>Requests</TableHead>
                      <TableHead>Limits</TableHead>
                      <TableHead>Usage</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <TableRow>
                      <TableCell className="font-medium">CPU</TableCell>
                      <TableCell>{service.resources?.requests?.cpu || "-"}</TableCell>
                      <TableCell>{service.resources?.limits?.cpu || "-"}</TableCell>
                      <TableCell>{service.current_usage?.cpu || "-"}</TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">Memory</TableCell>
                      <TableCell>{service.resources?.requests?.memory || "-"}</TableCell>
                      <TableCell>{service.resources?.limits?.memory || "-"}</TableCell>
                      <TableCell>{service.current_usage?.memory || "-"}</TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              </div>
            )}

            {/* Health Checks Section */}
            {hasHealthChecks && (
              <div>
                <SectionHeader>Health Checks</SectionHeader>
                <div className="space-y-1">
                  <InfoRow
                    label="Liveness"
                    value={formatProbeType(service.healthcheck?.livenessProbe)}
                  />
                  <InfoRow
                    label="Readiness"
                    value={formatProbeType(service.healthcheck?.readinessProbe)}
                  />
                </div>
              </div>
            )}

            {/* Auto-scaling Section */}
            {hasHPA && service.hpa && (
              <div>
                <SectionHeader>Auto-scaling</SectionHeader>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <Badge variant="outline" className="bg-green-500/10 text-green-500 border-green-500/30">
                    Enabled
                  </Badge>
                  <span className="text-muted-foreground">
                    Min: <span className="text-foreground font-medium">{service.hpa.minReplicas}</span>
                  </span>
                  <span className="text-muted-foreground">
                    Max: <span className="text-foreground font-medium">{service.hpa.maxReplicas}</span>
                  </span>
                  {hpaTargets.cpu && (
                    <span className="text-muted-foreground">
                      CPU: <span className="text-foreground font-medium">{hpaTargets.cpu}%</span>
                    </span>
                  )}
                  {hpaTargets.memory && (
                    <span className="text-muted-foreground">
                      Memory: <span className="text-foreground font-medium">{hpaTargets.memory}%</span>
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* Instances Section */}
            {hasPods && service.pods && (
              <div>
                <SectionHeader>Instances ({service.pods.length})</SectionHeader>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Ready</TableHead>
                      <TableHead>CPU</TableHead>
                      <TableHead>Memory</TableHead>
                      <TableHead>Restarts</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {service.pods.map((pod) => (
                      <TableRow key={pod.name}>
                        <TableCell className="font-mono text-xs truncate max-w-[120px]" title={pod.name}>
                          {pod.name.length > 20 ? `${pod.name.slice(0, 17)}...` : pod.name}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="outline"
                            className={`text-xs capitalize ${getStatusColor(pod.phase)}`}
                          >
                            {pod.phase}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-xs">
                          {pod.ready_containers}/{pod.total_containers}
                        </TableCell>
                        <TableCell className="text-xs">
                          {pod.cpu_usage || "-"}
                        </TableCell>
                        <TableCell className="text-xs">
                          {pod.memory_usage || "-"}
                        </TableCell>
                        <TableCell className={`text-xs ${pod.restart_count > 0 ? "text-destructive" : ""}`}>
                          {pod.restart_count}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            {/* Empty state if no detailed info */}
            {!hasResources && !hasHealthChecks && !hasHPA && !hasPods && (
              <div className="text-center text-muted-foreground text-sm py-8">
                No additional details available for this service.
              </div>
            )}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
