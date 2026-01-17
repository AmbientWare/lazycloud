import type { DeploymentWithStatus, ServiceStatusSummary } from "@/interfaces/deployments";
import { StyledAccordionItem, StyledAccordionTrigger, StyledAccordionContent } from "@/components/shared/styled-accordion";
import { Badge } from "@/components/ui/badge";
import { Server, HardDrive, Network, ExternalLink, AlertTriangle, Copy, Check, Info } from "lucide-react";
import { Spinner } from "@/components/shared/spinner";
import { useState } from "react";
import { ServiceDetailsSheet } from "./service-details-sheet";

function DomainSetupNotice({
  customDomain,
  cnameTarget,
  domainStatus,
}: {
  customDomain: string;
  cnameTarget: string;
  domainStatus: string | null | undefined;
}) {
  const [copied, setCopied] = useState(false);

  const copyToClipboard = async () => {
    await navigator.clipboard.writeText(cnameTarget);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const getStatusLabel = () => {
    switch (domainStatus) {
      case "pending_validation":
        return "Pending DNS";
      case "initializing":
        return "Initializing";
      default:
        return "SSL Pending";
    }
  };

  return (
    <div className="mt-2 rounded-md border border-yellow-500/30 bg-yellow-500/5 p-2">
      <div className="flex items-start gap-2">
        <AlertTriangle className="h-3.5 w-3.5 text-yellow-500 shrink-0 mt-0.5" />
        <div className="min-w-0 flex-1 space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium text-yellow-500">{getStatusLabel()}</span>
          </div>
          <p className="text-xs text-muted-foreground">
            Add a CNAME record to your DNS:
          </p>
          <div className="flex items-center gap-2 text-xs">
            <code className="bg-muted px-1.5 py-0.5 rounded text-foreground truncate">
              {customDomain}
            </code>
            <span className="text-muted-foreground shrink-0">→</span>
            <code className="bg-muted px-1.5 py-0.5 rounded text-foreground">
              {cnameTarget}
            </code>
            <button
              onClick={copyToClipboard}
              className="text-muted-foreground hover:text-foreground transition-colors shrink-0 p-2 min-h-[44px] min-w-[44px] flex items-center justify-center"
              title="Copy CNAME target"
              aria-label="Copy CNAME target"
            >
              {copied ? (
                <Check className="h-4 w-4 text-green-500" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}


export function DeploymentCard({ deployment }: { deployment: DeploymentWithStatus }) {
  const { status, isLoading, service_count, volume_count, network_count } = deployment;
  const services = status?.services ?? [];
  const volumes = status?.volumes ?? [];
  const networks = status?.networks ?? [];
  const hasStatusData = status !== undefined && status !== null;

  const [selectedServiceName, setSelectedServiceName] = useState<string | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  // Look up service from live data so it updates with SSE
  const selectedService = selectedServiceName
    ? services.find((s) => s.name === selectedServiceName) ?? null
    : null;

  const openServiceDetails = (service: ServiceStatusSummary) => {
    setSelectedServiceName(service.name);
    setSheetOpen(true);
  };

  const getStateBadgeColor = (state: string) => {
    switch (state.toLowerCase()) {
      case "deployed":
        return "bg-green-500/10 text-green-500 border-green-500/30";
      case "deploying":
        return "bg-blue-500/10 text-blue-500 border-blue-500/30";
      case "failed":
        return "bg-red-500/10 text-red-500 border-red-500/30";
      case "deleting":
        return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
      case "deleted":
        return "";
      default:
        return "";
    }
  };

  const getServiceStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case "running":
        return "bg-green-500/10 text-green-500 border-green-500/30";
      case "pending":
        return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
      case "failed":
        return "bg-red-500/10 text-red-500 border-red-500/30";
      default:
        return "";
    }
  };

  if (isLoading) {
    return (
      <StyledAccordionItem value={deployment.id}>
        <StyledAccordionTrigger>
          <div className="flex w-full items-center justify-between pr-4">
            <div className="flex items-center gap-3">
              <span className="h-2 w-2 rounded-full bg-lazycloud"></span>
              <span className="group-data-[state=open]:text-lazycloud text-sm font-semibold transition-colors">
                {deployment.name}
              </span>
              <Badge
                variant="outline"
                className={`text-xs capitalize ${getStateBadgeColor(deployment.state)}`}
              >
                {deployment.state}
              </Badge>
            </div>
            <div className="text-muted-foreground hidden sm:flex items-center gap-4 text-sm">
              <div className="flex items-center gap-1.5">
                <Server className="h-4 w-4" />
                <span>{service_count}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <HardDrive className="h-4 w-4" />
                <span>{volume_count}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <Network className="h-4 w-4" />
                <span>{network_count}</span>
              </div>
            </div>
          </div>
        </StyledAccordionTrigger>
        <StyledAccordionContent>
          <div className="flex items-center justify-center">
            <Spinner size="md" />
          </div>
        </StyledAccordionContent>
      </StyledAccordionItem>
    );
  }

  const runningServices = services.filter(
    (s) => s.status.toLowerCase() === "running",
  ).length;

  return (
    <StyledAccordionItem value={deployment.id}>
      <StyledAccordionTrigger>
        <div className="flex w-full items-center justify-between pr-4">
          <div className="flex items-center gap-3">
            <span className="h-2 w-2 rounded-full bg-lazycloud"></span>
            <span className="group-data-[state=open]:text-lazycloud text-sm font-semibold transition-colors">
              {deployment.name}
            </span>
            <Badge
              variant="outline"
              className={`text-xs capitalize ${getStateBadgeColor(deployment.state)}`}
            >
              {deployment.state}
            </Badge>
          </div>
          <div className="text-muted-foreground hidden sm:flex items-center gap-4 text-sm">
            <div className="flex items-center gap-1.5">
              <Server className="h-4 w-4" />
              {status ? (
                <span>
                  {runningServices}/{services.length}
                </span>
              ) : (
                <span>{service_count}</span>
              )}
            </div>
            <div className="flex items-center gap-1.5">
              <HardDrive className="h-4 w-4" />
              <span>{status?.volumes !== undefined && status.volumes !== null ? volumes.length : volume_count}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <Network className="h-4 w-4" />
              <span>{status?.networks !== undefined && status.networks !== null ? networks.length : network_count}</span>
            </div>
          </div>
        </div>
      </StyledAccordionTrigger>
      <StyledAccordionContent>
        <div className="space-y-6 pt-2">
          <div className="grid grid-cols-1 gap-4 sm:gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {/* Services Section */}
            <div className="space-y-3">
              <div className="flex items-center gap-2 pb-1">
                <div className="bg-blue-500/10 flex h-7 w-7 items-center justify-center rounded-md">
                  <Server className="text-blue-500 h-4 w-4" />
                </div>
                <h4 className="text-sm font-semibold">Services</h4>
                <div className="ml-auto flex items-center gap-2">
                  {status?.ready_services !== undefined && (
                    <span className="text-muted-foreground text-xs">
                      {status.ready_services}/{status.total_services} ready
                    </span>
                  )}
                  <Badge variant="outline" className="text-xs">
                    {services.length}
                  </Badge>
                </div>
              </div>
              {!hasStatusData ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  Loading services...
                </div>
              ) : services.length === 0 ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  No services found
                </div>
              ) : (
                  <div className="space-y-2">
                    {services.map((service) => (
                      <div
                        key={service.name}
                        className="bg-muted/50 rounded-md border border-border/50 p-3 shadow-sm transition-colors"
                      >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex min-w-0 flex-1 items-center gap-2">
                          <Badge
                            variant="outline"
                            className={`shrink-0 text-xs ${getServiceStatusColor(service.status)}`}
                          >
                            {service.status}
                          </Badge>
                          <span className="truncate text-sm font-medium">
                            {service.name}
                          </span>
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <div className="text-muted-foreground text-xs tabular-nums">
                            {service.ready_replicas}/{service.total_replicas}
                          </div>
                          {service.restarts > 0 && (
                            <span className="text-destructive text-xs">
                              · {service.restarts} restarts
                            </span>
                          )}
                          <button
                            onClick={() => openServiceDetails(service)}
                            className="text-muted-foreground hover:text-foreground transition-colors p-2 min-h-[44px] min-w-[44px] flex items-center justify-center rounded hover:bg-muted"
                            title="View details"
                            aria-label="View service details"
                          >
                            <Info className="h-4 w-4" />
                          </button>
                        </div>
                      </div>
                      {service.endpoint && (
                        <a
                          href={`https://${service.endpoint}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="mt-2 flex items-center gap-1.5 text-xs text-cyan-500 hover:text-cyan-400 transition-colors"
                        >
                          <ExternalLink className="h-3 w-3" />
                          <span className="truncate">{service.endpoint}</span>
                        </a>
                      )}
                      {service.custom_domain && service.domain_status !== "active" && service.cname_target && (
                        <DomainSetupNotice
                          customDomain={service.custom_domain}
                          cnameTarget={service.cname_target}
                          domainStatus={service.domain_status}
                        />
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Volumes Section */}
            <div className="space-y-3">
              <div className="flex items-center gap-2 pb-1">
                <div className="bg-purple-500/10 flex h-7 w-7 items-center justify-center rounded-md">
                  <HardDrive className="text-purple-500 h-4 w-4" />
                </div>
                <h4 className="text-sm font-semibold">Volumes</h4>
                <div className="ml-auto flex items-center gap-2">
                  <Badge variant="outline" className="text-xs">
                    {volumes.length}
                  </Badge>
                </div>
              </div>
              {!hasStatusData ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  Loading volumes...
                </div>
              ) : volumes.length === 0 ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  No volumes found
                </div>
              ) : (
                  <div className="space-y-2">
                    {volumes.map((volume) => (
                      <div
                        key={volume.name}
                        className="bg-muted/50 rounded-md border border-border/50 p-3 shadow-sm transition-colors"
                      >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {volume.name}
                        </span>
                        <div className="text-muted-foreground shrink-0 text-xs capitalize">
                          {volume.storage_type.toLowerCase()}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Networks Section */}
            <div className="space-y-3">
              <div className="flex items-center gap-2 pb-1">
                <div className="bg-green-500/10 flex h-7 w-7 items-center justify-center rounded-md">
                  <Network className="text-green-500 h-4 w-4" />
                </div>
                <h4 className="text-sm font-semibold">Networks</h4>
                <div className="ml-auto flex items-center gap-2">
                  <Badge variant="outline" className="text-xs">
                    {networks.length}
                  </Badge>
                </div>
              </div>
              {!hasStatusData ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  Loading networks...
                </div>
              ) : networks.length === 0 ? (
                <div className="text-muted-foreground rounded-md border border-dashed p-4 text-center text-xs">
                  No networks found
                </div>
              ) : (
                <div className="space-y-2">
                  {networks.map((network) => (
                    <div
                      key={network.name}
                      className="bg-muted/50 rounded-md border border-border/50 p-3 shadow-sm transition-colors"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="min-w-0 flex-1 truncate text-sm font-medium">
                          {network.name}
                        </span>
                        <div className="flex shrink-0 items-center gap-2">
                          {network.driver && (
                            <Badge variant="outline" className="text-xs">
                              {network.driver}
                            </Badge>
                          )}
                          <Badge
                            variant="outline"
                            className={`text-xs ${
                              network.status.toLowerCase() === "active"
                                ? "bg-green-500/10 text-green-500 border-green-500/30"
                                : ""
                            }`}
                          >
                            {network.status}
                          </Badge>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </StyledAccordionContent>

      <ServiceDetailsSheet
        service={selectedService}
        open={sheetOpen}
        onOpenChange={setSheetOpen}
      />
    </StyledAccordionItem>
  );
}