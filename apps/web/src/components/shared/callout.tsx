import { cn } from "@/lib/utils";
import { Info, AlertTriangle, Lightbulb, AlertCircle } from "lucide-react";
import type { ReactNode } from "react";

type CalloutType = "note" | "warning" | "tip" | "danger";

interface CalloutProps {
  type?: CalloutType;
  title?: string;
  children: ReactNode;
  className?: string;
}

const calloutConfig: Record<
  CalloutType,
  { icon: typeof Info; colors: string; defaultTitle: string }
> = {
  note: {
    icon: Info,
    colors: "border-blue-500/50 bg-blue-500/10 text-blue-200 [&_strong]:text-blue-300",
    defaultTitle: "Note",
  },
  warning: {
    icon: AlertTriangle,
    colors: "border-yellow-500/50 bg-yellow-500/10 text-yellow-200 [&_strong]:text-yellow-300",
    defaultTitle: "Warning",
  },
  tip: {
    icon: Lightbulb,
    colors: "border-green-500/50 bg-green-500/10 text-green-200 [&_strong]:text-green-300",
    defaultTitle: "Tip",
  },
  danger: {
    icon: AlertCircle,
    colors: "border-red-500/50 bg-red-500/10 text-red-200 [&_strong]:text-red-300",
    defaultTitle: "Danger",
  },
};

export function Callout({
  type = "note",
  title,
  children,
  className,
}: CalloutProps) {
  const config = calloutConfig[type];
  const Icon = config.icon;

  return (
    <div
      className={cn(
        "my-6 flex gap-3 rounded-lg border-l-4 p-4",
        config.colors,
        className,
      )}
    >
      <Icon className="mt-0.5 h-5 w-5 shrink-0" />
      <div className="min-w-0 flex-1">
        {title && (
          <p className="mb-1 font-semibold">{title}</p>
        )}
        <div className="[&_p]:mt-0 [&_p:not(:first-child)]:mt-2">{children}</div>
      </div>
    </div>
  );
}

// Convenience components for common callout types
export function Note({ children, title, className }: Omit<CalloutProps, "type">) {
  return <Callout type="note" title={title} className={className}>{children}</Callout>;
}

export function Warning({ children, title, className }: Omit<CalloutProps, "type">) {
  return <Callout type="warning" title={title} className={className}>{children}</Callout>;
}

export function Tip({ children, title, className }: Omit<CalloutProps, "type">) {
  return <Callout type="tip" title={title} className={className}>{children}</Callout>;
}

export function Danger({ children, title, className }: Omit<CalloutProps, "type">) {
  return <Callout type="danger" title={title} className={className}>{children}</Callout>;
}
