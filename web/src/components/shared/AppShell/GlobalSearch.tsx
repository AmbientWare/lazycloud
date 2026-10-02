import { useDeferredValue, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import {
  Activity,
  AppWindow,
  Boxes,
  ChartNoAxesCombined,
  Database,
  CornerDownLeft,
  Search,
  Settings,
  SquareTerminal,
} from "lucide-react";

import {
  CommandDialog,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { ContentTransition } from "@/components/shared/ContentTransition";
import { formatKind, shortId } from "@/lib/format";
import {
  appSearchQueryOptions,
  workloadSearchQueryOptions,
  taskSearchQueryOptions,
  sandboxSearchQueryOptions,
} from "@/lib/queries/search";
import { useWorkspace } from "@/lib/workspace-context";

const GROUPS = ["Navigate", "Apps", "Workloads", "Tasks", "Sandboxes"] as const;

type SearchResult = {
  group: (typeof GROUPS)[number];
  key: string;
  label: string;
  detail: string;
  href: string;
  icon: typeof Search;
};

export function GlobalSearch({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const router = useRouter();
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.trim().slice(0, 100));
  const normalizedQuery = deferredQuery.toLowerCase();

  // The server answers every resource match; only the fixed destinations below are
  // matched here.
  const apps = useQuery({ ...appSearchQueryOptions(workspace.name, deferredQuery), enabled: open });
  const workloads = useQuery({
    ...workloadSearchQueryOptions(workspace.name, deferredQuery),
    enabled: open,
  });
  const tasks = useQuery({
    ...taskSearchQueryOptions(workspace.name, deferredQuery),
    enabled: open && deferredQuery.length >= 2,
  });
  const sandboxes = useQuery({
    ...sandboxSearchQueryOptions(workspace.name, deferredQuery),
    enabled: open,
  });

  const results = useMemo(() => {
    const base = `/w/${encodeURIComponent(workspace.name)}`;
    const matches = (value: string) =>
      !normalizedQuery || value.toLowerCase().includes(normalizedQuery);
    const next: SearchResult[] = [];

    const destinations: SearchResult[] = [
      {
        key: "destination-apps",
        group: "Navigate",
        label: "Apps",
        detail: "",
        href: `${base}/apps`,
        icon: Boxes,
      },
      {
        key: "destination-tasks",
        group: "Navigate",
        label: "Tasks",
        detail: "",
        href: `${base}/tasks`,
        icon: Activity,
      },
      {
        key: "destination-storage",
        group: "Navigate",
        label: "Storage",
        detail: "",
        href: `${base}/storage`,
        icon: Database,
      },
      {
        key: "destination-usage",
        group: "Navigate",
        label: "Usage",
        detail: "",
        href: `${base}/usage`,
        icon: ChartNoAxesCombined,
      },
      {
        key: "destination-settings",
        group: "Navigate",
        label: "Settings",
        detail: "",
        href: `${base}/apps?settings=billing`,
        icon: Settings,
      },
    ];
    next.push(...destinations.filter((item) => matches(`${item.label} ${item.detail}`)));

    for (const app of apps.data ?? []) {
      next.push({
        key: `app-${app.id}`,
        group: "Apps",
        label: app.name,
        detail: `${app.workloads} ${app.workloads === 1 ? "workload" : "workloads"}`,
        href: `${base}/apps/${encodeURIComponent(app.name)}`,
        icon: AppWindow,
      });
    }

    for (const workload of workloads.data ?? []) {
      next.push({
        key: `workload-${workload.id}`,
        group: "Workloads",
        label: workload.name,
        detail: formatKind(workload.kind),
        href: `${base}/apps/${encodeURIComponent(workload.app)}/workloads/${encodeURIComponent(workload.kind)}/${encodeURIComponent(workload.name)}`,
        icon: Boxes,
      });
    }

    for (const task of tasks.data ?? []) {
      next.push({
        key: `task-${task.id}`,
        group: "Tasks",
        label: task.function,
        detail: `${formatKind(task.status)} · ${shortId(task.id)}`,
        href: `${base}/tasks/${encodeURIComponent(task.id)}`,
        icon: Activity,
      });
    }

    for (const sandbox of sandboxes.data ?? []) {
      next.push({
        key: `sandbox-${sandbox.id}`,
        group: "Sandboxes",
        label: sandbox.name,
        detail: `${formatKind(sandbox.status)} · ${shortId(sandbox.id)}`,
        href: `${base}/sandboxes/${encodeURIComponent(sandbox.id)}`,
        icon: SquareTerminal,
      });
    }

    return next.slice(0, 30);
  }, [apps.data, normalizedQuery, sandboxes.data, tasks.data, workspace.name, workloads.data]);

  // Results arrive in group order. When the highlighted result drops out of the
  // list, highlight the first one so Enter always opens something.
  const ordered = useMemo(
    () => GROUPS.flatMap((group) => results.filter((result) => result.group === group)),
    [results],
  );
  const [selected, setSelected] = useState("");
  const highlighted = ordered.some((result) => result.key === selected)
    ? selected
    : (ordered[0]?.key ?? "");

  const openResult = (result: SearchResult) => {
    onOpenChange(false);
    setQuery("");
    router.history.push(result.href);
  };

  const loading =
    apps.isPending ||
    workloads.isPending ||
    sandboxes.isPending ||
    (deferredQuery.length >= 2 && tasks.isPending);
  const partialError = apps.isError || workloads.isError || tasks.isError || sandboxes.isError;

  return (
    <CommandDialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) setQuery("");
        onOpenChange(nextOpen);
      }}
      title="Search workspace"
      description="Search this workspace by name or ID."
      shouldFilter={false}
      value={highlighted}
      onValueChange={setSelected}
      className="top-[12svh] flex max-h-[76svh] w-[calc(100%-1.5rem)] translate-y-0 gap-0 border-border bg-popover shadow-2xl sm:max-w-xl"
    >
      <CommandInput
        autoFocus
        value={query}
        onValueChange={setQuery}
        placeholder="Search workspace…"
        aria-label="Search workspace"
        className="pr-8"
      />
      <CommandList
        data-search-results-scroll=""
        className="h-[min(26rem,var(--cmdk-list-height))] max-h-[calc(76svh-5.5rem)] min-h-24 scroll-py-2 p-2 transition-[height] duration-150 motion-reduce:transition-none"
      >
        {GROUPS.map((group) => {
          const items = results.filter((result) => result.group === group);
          if (!items.length) return null;
          return (
            <CommandGroup key={group} heading={group} className="p-0 pb-2 last:pb-0">
              {items.map((result) => {
                const Icon = result.icon;
                return (
                  <CommandItem
                    key={result.key}
                    value={result.key}
                    onSelect={() => openResult(result)}
                    className="group min-h-9 gap-2.5 rounded-md px-2 py-2 text-muted-foreground data-[selected=true]:bg-accent data-[selected=true]:text-foreground data-[selected=true]:ring-1 data-[selected=true]:ring-inset data-[selected=true]:ring-border"
                  >
                    <Icon className="size-4 shrink-0" aria-hidden="true" />
                    <span className="min-w-0 flex-1 truncate text-sm text-foreground">
                      {result.label}
                    </span>
                    <span className="max-w-[45%] truncate text-xs text-muted-foreground">
                      {result.detail}
                    </span>
                    <CornerDownLeft
                      className="size-3.5 opacity-0 group-data-[selected=true]:opacity-100"
                      aria-hidden="true"
                    />
                  </CommandItem>
                );
              })}
            </CommandGroup>
          );
        })}
        {loading ? (
          <ContentTransition
            pending
            role="status"
            className="px-2 py-3 text-xs text-muted-foreground"
          >
            Searching…
          </ContentTransition>
        ) : results.length === 0 ? (
          <p
            role="status"
            className="content-transition px-2 py-8 text-center text-sm text-muted-foreground"
          >
            No matching resources
          </p>
        ) : null}
        {partialError ? (
          <p className="px-3 py-2 text-xs text-warning">Some resource results are unavailable.</p>
        ) : null}
      </CommandList>
      <div
        className="flex shrink-0 items-center gap-4 border-t px-4 py-2 text-[11px] text-muted-foreground"
        aria-hidden="true"
      >
        <span>
          <kbd className="mr-1.5 font-sans">↑ ↓</kbd>Navigate
        </span>
        <span>
          <kbd className="mr-1.5 font-sans">↵</kbd>Open
        </span>
        <span className="ml-auto">
          <kbd className="mr-1.5 font-sans">Esc</kbd>Close
        </span>
      </div>
    </CommandDialog>
  );
}
