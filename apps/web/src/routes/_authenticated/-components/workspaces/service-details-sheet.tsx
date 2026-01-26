import type {
  ServiceStatusSummary,
  ProbeConfig,
} from '@/interfaces/deployments'
import {
  Drawer,
  DrawerContent,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  ExternalLink,
  Copy,
  Check,
  Cpu,
  Activity,
  Server,
  Scaling,
} from 'lucide-react'
import { useState } from 'react'

interface ServiceDetailsSheetProps {
  service: ServiceStatusSummary | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

function getStatusColor(status: string) {
  switch (status.toLowerCase()) {
    case 'running':
      return 'bg-green-500/10 text-green-500 border-green-500/30'
    case 'pending':
    case 'creating':
    case 'health_check':
    case 'updating':
      return 'bg-yellow-500/10 text-yellow-500 border-yellow-500/30'
    case 'error':
    case 'failed':
    case 'restarting':
      return 'bg-red-500/10 text-red-500 border-red-500/30'
    case 'exited':
    case 'stopping':
      return 'bg-muted text-muted-foreground border-border'
    default:
      return ''
  }
}

function formatProbeType(probe: ProbeConfig | null | undefined): string {
  if (!probe) return 'Not configured'
  if (probe.httpGet) {
    return `HTTP ${probe.httpGet.path}:${probe.httpGet.port}`
  }
  if (probe.tcpSocket) {
    return `TCP :${probe.tcpSocket.port}`
  }
  if (probe.exec) {
    return 'Exec'
  }
  return 'Configured'
}

function getStatusDisplay(service: ServiceStatusSummary): string {
  const hasHealthCheck =
    service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe
  if (service.status.toLowerCase() === 'running' && !hasHealthCheck) {
    return 'Running (no health check)'
  }
  return service.status
}

function SectionCard({
  icon: Icon,
  title,
  iconColor,
  children,
}: {
  icon: React.ElementType
  title: string
  iconColor: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-border/50 bg-muted/30 p-4">
      <div className="mb-3 flex items-center gap-2">
        <div
          className={`flex size-6 items-center justify-center rounded-md ${iconColor}`}
        >
          <Icon className="h-3.5 w-3.5" />
        </div>
        <h4 className="text-sm font-semibold">{title}</h4>
      </div>
      {children}
    </div>
  )
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between py-1">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xs font-medium">{value}</span>
    </div>
  )
}

export function ServiceDetailsSheet({
  service,
  open,
  onOpenChange,
}: ServiceDetailsSheetProps) {
  const [copied, setCopied] = useState(false)

  if (!service) return null

  const copyEndpoint = async () => {
    if (service.endpoint) {
      await navigator.clipboard.writeText(`https://${service.endpoint}`)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  const hasResources =
    service.resources?.requests ||
    service.resources?.limits ||
    service.current_usage
  const hasHealthChecks =
    service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe
  const hasHPA = service.hpa?.enabled
  const hasPods = service.pods && service.pods.length > 0

  const getHPATargets = () => {
    if (!service.hpa?.metrics) return { cpu: null, memory: null }
    let cpu: number | null = null
    let memory: number | null = null
    for (const metric of service.hpa.metrics) {
      if (metric.type === 'Resource' && metric.resource) {
        const name = metric.resource.name as string
        const target = metric.resource.target as {
          averageUtilization?: number
        }
        if (name === 'cpu' && target?.averageUtilization) {
          cpu = target.averageUtilization
        }
        if (name === 'memory' && target?.averageUtilization) {
          memory = target.averageUtilization
        }
      }
    }
    return { cpu, memory }
  }

  const hpaTargets = getHPATargets()

  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <DrawerContent className="max-h-[96dvh] sm:max-h-[90vh]">
        <div className="mx-auto w-full max-w-3xl">
          <DrawerHeader className="pb-2">
            <div className="flex items-center gap-3">
              <Badge
                variant="outline"
                className={`text-xs ${getStatusColor(service.status)}`}
              >
                {getStatusDisplay(service)}
              </Badge>
              <DrawerTitle>{service.name}</DrawerTitle>
            </div>
          </DrawerHeader>

          <Separator />

          <div className="overflow-y-auto p-4 pb-12 sm:pb-8">
            {/* Overview row */}
            <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 sm:gap-x-6">
              <div className="text-sm">
                <span className="text-muted-foreground">Replicas: </span>
                <span className="font-medium">
                  {service.ready_replicas}/{service.total_replicas}
                </span>
              </div>
              {service.ports && service.ports.length > 0 && (
                <div className="text-sm">
                  <span className="text-muted-foreground">Ports: </span>
                  <span className="font-medium">
                    {service.ports.join(', ')}
                  </span>
                </div>
              )}
              {service.restarts > 0 && (
                <div className="text-sm">
                  <span className="text-muted-foreground">Restarts: </span>
                  <span className="font-medium text-destructive">
                    {service.restarts}
                  </span>
                </div>
              )}
            </div>

            {/* Endpoint */}
            {service.endpoint && (
              <div className="mb-6 flex items-center gap-2 rounded-lg border border-border/50 bg-muted/50 p-3">
                <a
                  href={`https://${service.endpoint}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-2 truncate text-sm text-cyan-500 transition-colors hover:text-cyan-400"
                >
                  <ExternalLink className="size-4 shrink-0" />
                  <span className="truncate">{service.endpoint}</span>
                </a>
                <button
                  onClick={copyEndpoint}
                  className="ml-auto shrink-0 rounded p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  title="Copy endpoint"
                  aria-label="Copy endpoint"
                >
                  {copied ? (
                    <Check className="size-4 text-green-500" />
                  ) : (
                    <Copy className="size-4" />
                  )}
                </button>
              </div>
            )}

            {/* Grid of sections */}
            <div className="grid gap-4 sm:grid-cols-2">
              {/* Resources */}
              {hasResources && (
                <SectionCard
                  icon={Cpu}
                  title="Resources"
                  iconColor="bg-blue-500/10 text-blue-500"
                >
                  <div className="space-y-1">
                    <div className="hidden grid-cols-4 gap-2 border-b border-border/30 pb-1 text-xs text-muted-foreground sm:grid">
                      <span></span>
                      <span>Request</span>
                      <span>Limit</span>
                      <span>Usage</span>
                    </div>
                    <div className="hidden grid-cols-4 gap-2 py-1 text-xs sm:grid">
                      <span className="text-muted-foreground">CPU</span>
                      <span>{service.resources?.requests?.cpu || '-'}</span>
                      <span>{service.resources?.limits?.cpu || '-'}</span>
                      <span>{service.current_usage?.cpu || '-'}</span>
                    </div>
                    <div className="hidden grid-cols-4 gap-2 py-1 text-xs sm:grid">
                      <span className="text-muted-foreground">Memory</span>
                      <span>{service.resources?.requests?.memory || '-'}</span>
                      <span>{service.resources?.limits?.memory || '-'}</span>
                      <span>{service.current_usage?.memory || '-'}</span>
                    </div>
                    {/* Mobile-friendly stacked layout */}
                    <div className="space-y-3 sm:hidden">
                      <div>
                        <span className="text-xs text-muted-foreground">
                          CPU
                        </span>
                        <div className="mt-1 grid grid-cols-3 gap-2 text-xs">
                          <div>
                            <span className="text-muted-foreground">Req:</span>{' '}
                            {service.resources?.requests?.cpu || '-'}
                          </div>
                          <div>
                            <span className="text-muted-foreground">Lim:</span>{' '}
                            {service.resources?.limits?.cpu || '-'}
                          </div>
                          <div>
                            <span className="text-muted-foreground">Use:</span>{' '}
                            {service.current_usage?.cpu || '-'}
                          </div>
                        </div>
                      </div>
                      <div>
                        <span className="text-xs text-muted-foreground">
                          Memory
                        </span>
                        <div className="mt-1 grid grid-cols-3 gap-2 text-xs">
                          <div>
                            <span className="text-muted-foreground">Req:</span>{' '}
                            {service.resources?.requests?.memory || '-'}
                          </div>
                          <div>
                            <span className="text-muted-foreground">Lim:</span>{' '}
                            {service.resources?.limits?.memory || '-'}
                          </div>
                          <div>
                            <span className="text-muted-foreground">Use:</span>{' '}
                            {service.current_usage?.memory || '-'}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </SectionCard>
              )}

              {/* Health Checks */}
              {hasHealthChecks && (
                <SectionCard
                  icon={Activity}
                  title="Health Checks"
                  iconColor="bg-green-500/10 text-green-500"
                >
                  <div className="space-y-1">
                    <InfoRow
                      label="Liveness"
                      value={formatProbeType(
                        service.healthcheck?.livenessProbe,
                      )}
                    />
                    <InfoRow
                      label="Readiness"
                      value={formatProbeType(
                        service.healthcheck?.readinessProbe,
                      )}
                    />
                  </div>
                </SectionCard>
              )}

              {/* Auto-scaling */}
              {hasHPA && service.hpa && (
                <SectionCard
                  icon={Scaling}
                  title="Auto-scaling"
                  iconColor="bg-purple-500/10 text-purple-500"
                >
                  <div className="flex flex-wrap gap-2">
                    <Badge
                      variant="outline"
                      className="border-green-500/30 bg-green-500/10 text-xs text-green-500"
                    >
                      Enabled
                    </Badge>
                    <span className="text-xs text-muted-foreground">
                      Min:{' '}
                      <span className="font-medium text-foreground">
                        {service.hpa.minReplicas}
                      </span>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      Max:{' '}
                      <span className="font-medium text-foreground">
                        {service.hpa.maxReplicas}
                      </span>
                    </span>
                    {hpaTargets.cpu && (
                      <span className="text-xs text-muted-foreground">
                        CPU:{' '}
                        <span className="font-medium text-foreground">
                          {hpaTargets.cpu}%
                        </span>
                      </span>
                    )}
                    {hpaTargets.memory && (
                      <span className="text-xs text-muted-foreground">
                        Mem:{' '}
                        <span className="font-medium text-foreground">
                          {hpaTargets.memory}%
                        </span>
                      </span>
                    )}
                  </div>
                </SectionCard>
              )}
            </div>

            {/* Instances Table */}
            {hasPods && service.pods && (
              <div className="mt-4">
                <SectionCard
                  icon={Server}
                  title={`Instances (${service.pods.length})`}
                  iconColor="bg-orange-500/10 text-orange-500"
                >
                  <div className="-mx-1 overflow-x-auto">
                    <Table>
                      <TableHeader>
                        <TableRow className="border-border/30">
                          <TableHead className="h-8 text-xs">Name</TableHead>
                          <TableHead className="h-8 text-xs">Status</TableHead>
                          <TableHead className="h-8 text-xs">Ready</TableHead>
                          <TableHead className="h-8 text-xs">CPU</TableHead>
                          <TableHead className="h-8 text-xs">Memory</TableHead>
                          <TableHead className="h-8 text-xs">
                            Restarts
                          </TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {service.pods.map((pod) => (
                          <TableRow key={pod.name} className="border-border/30">
                            <TableCell
                              className="max-w-[150px] truncate py-2 font-mono text-xs"
                              title={pod.name}
                            >
                              {pod.name.length > 24
                                ? `${pod.name.slice(0, 21)}...`
                                : pod.name}
                            </TableCell>
                            <TableCell className="py-2">
                              <Badge
                                variant="outline"
                                className={`text-xs capitalize ${getStatusColor(pod.phase)}`}
                              >
                                {pod.phase}
                              </Badge>
                            </TableCell>
                            <TableCell className="py-2 text-xs">
                              {pod.ready_containers}/{pod.total_containers}
                            </TableCell>
                            <TableCell className="py-2 text-xs">
                              {pod.cpu_usage || '-'}
                            </TableCell>
                            <TableCell className="py-2 text-xs">
                              {pod.memory_usage || '-'}
                            </TableCell>
                            <TableCell
                              className={`py-2 text-xs ${pod.restart_count > 0 ? 'text-destructive' : ''}`}
                            >
                              {pod.restart_count}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </SectionCard>
              </div>
            )}

            {/* Empty state */}
            {!hasResources && !hasHealthChecks && !hasHPA && !hasPods && (
              <div className="flex flex-col items-center justify-center py-8 text-center">
                <div className="mb-3 flex size-10 items-center justify-center rounded-lg bg-muted">
                  <Server className="size-5 text-muted-foreground" />
                </div>
                <p className="text-sm text-muted-foreground">
                  No additional details available
                </p>
              </div>
            )}
          </div>
        </div>
      </DrawerContent>
    </Drawer>
  )
}
