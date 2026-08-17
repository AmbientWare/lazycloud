import { Clock, Globe, Layers, Terminal, Zap, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";

const KIND_ICONS: Record<string, { icon: LucideIcon; label: string }> = {
  function: { icon: Zap, label: "Function" },
  command: { icon: Terminal, label: "Command" },
  endpoint: { icon: Globe, label: "Endpoint" },
  asgi: { icon: Globe, label: "ASGI endpoint" },
  "cron-job": { icon: Clock, label: "Schedule" },
  pod: { icon: Layers, label: "Pod" },
  sandbox: { icon: Terminal, label: "Sandbox" },
  shell: { icon: Terminal, label: "Shell" },
};

export function StubKindIcon({
  kind,
  className,
}: {
  kind: string | undefined;
  className?: string;
}) {
  const entry = kind ? KIND_ICONS[kind] : undefined;
  const Icon = entry?.icon ?? Zap;
  return (
    <Icon
      className={cn("size-3.5 shrink-0 text-muted-foreground", className)}
      aria-label={entry?.label ?? "Workload"}
    />
  );
}
