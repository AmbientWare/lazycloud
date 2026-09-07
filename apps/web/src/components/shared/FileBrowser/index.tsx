import type { ReactNode } from "react";
import { ChevronRight, File, Folder } from "lucide-react";

import { LiveRelativeTime } from "@/components/shared/LiveTime";
import { RowsSkeleton } from "@/components/shared/RowsSkeleton";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

export function FileBreadcrumbs({
  path,
  rootLabel,
  absolute = false,
  onNavigate,
}: {
  path: string;
  rootLabel: string;
  absolute?: boolean;
  onNavigate: (path: string) => void;
}) {
  const root = absolute ? "/" : "";
  const crumbs = [{ label: rootLabel, path: root }];
  let current = "";
  for (const part of path.split("/").filter(Boolean)) {
    current = current ? `${current}/${part}` : `${absolute ? "/" : ""}${part}`;
    crumbs.push({ label: part, path: current });
  }
  return (
    <nav
      aria-label="File path"
      className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto text-xs"
    >
      {crumbs.map((crumb, index) => (
        <span key={crumb.path} className="flex shrink-0 items-center gap-1">
          {index > 0 ? (
            <ChevronRight className="size-3 text-muted-foreground" aria-hidden="true" />
          ) : null}
          <button
            type="button"
            aria-current={crumb.path === path ? "location" : undefined}
            className={cn(
              "interactive-link mono",
              crumb.path === path ? "text-foreground" : "text-muted-foreground",
            )}
            onClick={() => onNavigate(crumb.path)}
          >
            {crumb.label}
          </button>
        </span>
      ))}
    </nav>
  );
}

export function FileRow({
  name,
  directory,
  size,
  modifiedAt,
  selected,
  onOpen,
  children,
}: {
  name: string;
  directory: boolean;
  size: number;
  modifiedAt?: string;
  selected?: boolean;
  onOpen?: () => void;
  children?: ReactNode;
}) {
  const Icon = directory ? Folder : File;
  return (
    <div
      className="interactive-row group flex min-w-0 items-center gap-2 px-3 py-2"
      data-selected={selected}
    >
      <Icon
        className={cn("size-4 shrink-0", directory ? "text-brand" : "text-muted-foreground")}
        aria-hidden="true"
      />
      {onOpen ? (
        <button
          type="button"
          onClick={onOpen}
          aria-pressed={directory ? undefined : selected}
          className="interactive-link mono min-w-0 flex-1 truncate text-left text-[13px]"
          title={name}
        >
          {name}
        </button>
      ) : (
        <span className="mono min-w-0 flex-1 truncate text-[13px]" title={name}>
          {name}
        </span>
      )}
      {modifiedAt ? (
        <span className="hidden shrink-0 text-[11px] text-muted-foreground sm:inline">
          <LiveRelativeTime value={modifiedAt} />
        </span>
      ) : null}
      {!directory ? (
        <span className="mono shrink-0 text-right text-[11px] text-muted-foreground">
          {formatBytes(size)}
        </span>
      ) : null}
      {children ? <span className="flex shrink-0 items-center gap-1">{children}</span> : null}
    </div>
  );
}

export function FileRowsSkeleton() {
  return <RowsSkeleton rows={5} height="h-6" />;
}
