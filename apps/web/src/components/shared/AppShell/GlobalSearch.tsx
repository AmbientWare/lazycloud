import { useDeferredValue, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import {
  Activity,
  AppWindow,
  Boxes,
  ChartNoAxesCombined,
  Database,
  Search,
  Settings,
  SquareTerminal,
} from "lucide-react";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { sandboxesQueryOptions } from "@/lib/queries/sandboxes";
import { stubsQueryOptions } from "@/lib/queries/stubs";
import { tasksQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

type SearchResult = {
  key: string;
  label: string;
  detail: string;
  href: string;
  icon: typeof Search;
};

export function GlobalSearch({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { workspace } = useWorkspace();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.trim());
  const normalizedQuery = deferredQuery.toLowerCase();

  const apps = useQuery({ ...appSummariesQueryOptions(workspace.id), enabled: open });
  const workloads = useQuery({ ...stubsQueryOptions(workspace.id), enabled: open });
  const tasks = useQuery({
    ...tasksQueryOptions(workspace.id, { limit: 10, search: deferredQuery }),
    enabled: open && deferredQuery.length >= 2,
  });
  const sandboxes = useQuery({ ...sandboxesQueryOptions(workspace.id), enabled: open });

  const results = useMemo(() => {
    const base = `/w/${encodeURIComponent(workspace.name)}`;
    const matches = (value: string) => !normalizedQuery || value.toLowerCase().includes(normalizedQuery);
    const next: SearchResult[] = [];

    const destinations: SearchResult[] = [
      { key: "destination-apps", label: "Apps", detail: "Workspace", href: `${base}/apps`, icon: Boxes },
      { key: "destination-tasks", label: "Tasks", detail: "Workspace", href: `${base}/tasks`, icon: Activity },
      { key: "destination-storage", label: "Storage", detail: "Workspace", href: `${base}/storage`, icon: Database },
      { key: "destination-usage", label: "Usage", detail: "Workspace", href: `${base}/usage`, icon: ChartNoAxesCombined },
      { key: "destination-settings", label: "Settings", detail: "Workspace", href: `${base}/settings`, icon: Settings },
    ];
    next.push(...destinations.filter((item) => matches(`${item.label} ${item.detail}`)));

    for (const item of apps.data?.items ?? []) {
      if (!matches(`${item.app.name} ${item.app.id}`)) continue;
      next.push({
        key: `app-${item.app.id}`,
        label: item.app.name,
        detail: `App · ${item.workload_count} ${item.workload_count === 1 ? "workload" : "workloads"}`,
        href: `${base}/apps/${encodeURIComponent(item.app.id)}`,
        icon: AppWindow,
      });
    }

    for (const workload of workloads.data?.stubs ?? []) {
      if (!workload.app_id || !matches(`${workload.name} ${workload.handler ?? ""} ${workload.id} ${workload.kind}`)) continue;
      next.push({
        key: `workload-${workload.id}`,
        label: workload.name,
        detail: workload.handler
          ? `${formatKind(workload.kind)} · ${workload.handler}`
          : formatKind(workload.kind),
        href: `${base}/apps/${encodeURIComponent(workload.app_id)}/workloads/${encodeURIComponent(workload.name)}`,
        icon: Boxes,
      });
    }

    for (const task of tasks.data?.data ?? []) {
      next.push({
        key: `task-${task.id}`,
        label: task.name || "Task",
        detail: `Task · ${formatKind(task.workload?.kind ?? "workload")} · ${formatKind(task.status)}`,
        href: `${base}/tasks/${encodeURIComponent(task.id)}`,
        icon: Activity,
      });
    }

    for (const sandbox of sandboxes.data?.data ?? []) {
      if (!sandbox.container_id || !matches(`${sandbox.name} ${sandbox.id} ${sandbox.container_id}`)) continue;
      next.push({
        key: `sandbox-${sandbox.id}-${sandbox.container_id}`,
        label: sandbox.name,
        detail: `Sandbox · ${formatKind(sandbox.status)}`,
        href: `${base}/sandboxes/${encodeURIComponent(sandbox.container_id)}`,
        icon: SquareTerminal,
      });
    }

    return next.slice(0, 30);
  }, [apps.data, normalizedQuery, tasks.data, sandboxes.data, workspace.name, workloads.data]);

  const openResult = (result: SearchResult) => {
    onOpenChange(false);
    setQuery("");
    router.history.push(result.href);
  };

  const loading = apps.isPending || workloads.isPending || sandboxes.isPending || (deferredQuery.length >= 2 && tasks.isPending);
  const partialError = apps.isError || workloads.isError || sandboxes.isError || tasks.isError;

  return (
    <CommandDialog
      open={open}
      onOpenChange={(nextOpen) => {
      if (!nextOpen) setQuery("");
      onOpenChange(nextOpen);
      }}
      title="Search workspace"
      description="Find apps, workloads, tasks, sandboxes, and workspace destinations"
      className="top-[10svh] max-h-[75svh] w-[calc(100%-1.5rem)] max-w-2xl translate-y-0 border-border bg-popover shadow-2xl"
    >
      <CommandInput
        autoFocus
        value={query}
        onValueChange={setQuery}
        placeholder="Search apps, workloads, tasks, sandboxes"
        className="pr-10"
      />
      <CommandList
        data-search-results-scroll=""
        className="max-h-[calc(75svh-3rem)] min-h-24 p-2"
      >
        <CommandEmpty>
          {loading ? "Searching workspace" : "No matching resources"}
        </CommandEmpty>
        <CommandGroup>
          {results.map((result) => {
            const Icon = result.icon;
            return (
              <CommandItem
                key={result.key}
                value={`${result.key} ${result.label} ${result.detail}`}
                onSelect={() => openResult(result)}
                className="min-h-11 gap-3 px-3 py-2 text-muted-foreground data-[selected=true]:bg-accent/70 data-[selected=true]:text-foreground"
              >
                <Icon className="size-4 shrink-0" aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-foreground">
                    {result.label}
                  </span>
                  <span className="mono block truncate text-[11px] text-muted-foreground">
                    {result.detail}
                  </span>
                </span>
              </CommandItem>
            );
          })}
        </CommandGroup>
        {loading && results.length > 0 ? (
          <p className="px-3 py-2 text-xs text-muted-foreground">Searching workspace</p>
        ) : null}
        {partialError ? (
          <p className="px-3 py-2 text-xs text-warning">Some resource results are unavailable.</p>
        ) : null}
      </CommandList>
    </CommandDialog>
  );
}

function formatKind(kind: string): string {
  return kind
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
