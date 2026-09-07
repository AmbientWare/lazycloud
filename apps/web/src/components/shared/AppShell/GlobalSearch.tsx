import { useDeferredValue, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
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

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import type { ResourceSearchResult } from "@/lib/api/schemas/search";
import { resourceSearchQueryOptions } from "@/lib/queries/search";
import { useWorkspace } from "@/lib/workspace-context";

const resourceIcons = { app: AppWindow, workload: Boxes, task: Activity, sandbox: SquareTerminal };
const resourceLabels = { app: "App", workload: "Workload", task: "Task", sandbox: "Sandbox" };

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
  const deferredQuery = useDeferredValue(query.trim());
  const resources = useInfiniteQuery(resourceSearchQueryOptions(workspace.id, deferredQuery));
  const base = `/w/${encodeURIComponent(workspace.name)}`;
  const destinations = [
    { label: "Apps", href: `${base}/apps`, icon: Boxes },
    { label: "Tasks", href: `${base}/tasks`, icon: Activity },
    { label: "Storage", href: `${base}/storage`, icon: Database },
    { label: "Usage", href: `${base}/usage`, icon: ChartNoAxesCombined },
    { label: "Settings", href: "settings", icon: Settings },
  ].filter((destination) => destination.label.toLowerCase().includes(query.trim().toLowerCase()));
  const results = resources.data?.pages.flatMap((page) => page.data) ?? [];
  const searching = deferredQuery.length > 0 && resources.isPending;

  const openDestination = (href: string) => {
    onOpenChange(false);
    if (href === "settings") {
      void router.navigate({
        to: ".",
        search: (previous) => ({ ...previous, settings: "general" }),
      });
    } else {
      router.history.push(href);
    }
  };

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      shouldFilter={false}
      title="Search workspace"
      description="Find apps, workloads, tasks, and sandboxes"
      className="top-[10svh] max-h-[75svh] w-[calc(100%-1.5rem)] max-w-2xl translate-y-0"
    >
      <CommandInput
        autoFocus
        value={query}
        maxLength={240}
        onValueChange={setQuery}
        placeholder="Search workspace"
        className="pr-10"
      />
      <CommandList data-search-results-scroll="" className="max-h-[calc(75svh-3rem)] min-h-24 p-2">
        {!resources.isError ? (
          <CommandEmpty>{searching ? "Searching workspace" : "No matching resources"}</CommandEmpty>
        ) : null}
        {destinations.length > 0 ? (
          <CommandGroup heading={query ? "Pages" : undefined}>
            {destinations.map((destination) => (
              <SearchItem
                key={destination.href}
                id={destination.href}
                label={destination.label}
                icon={destination.icon}
                onSelect={() => openDestination(destination.href)}
              />
            ))}
          </CommandGroup>
        ) : null}
        {deferredQuery ? (
          <CommandGroup heading={results.length > 0 ? "Resources" : undefined}>
            {results.map((result) => (
              <SearchItem
                key={`${result.kind}-${result.id}`}
                id={`${result.kind}-${result.id}`}
                label={result.name || resourceLabels[result.kind]}
                detail={
                  result.kind === "workload" ? result.workload_kind : resourceLabels[result.kind]
                }
                icon={resourceIcons[result.kind]}
                onSelect={() => openDestination(resourceHref(base, result))}
              />
            ))}
          </CommandGroup>
        ) : null}
        {resources.isError && !resources.isFetchNextPageError ? (
          <ApiErrorNotice
            compact
            error={resources.error}
            title="Search could not be completed"
            onRetry={() => void resources.refetch()}
            retrying={resources.isFetching}
          />
        ) : null}
        {searching && destinations.length > 0 ? (
          <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
            Searching workspace
          </p>
        ) : null}
        {deferredQuery && resources.data ? (
          <InfiniteScrollBoundary
            key={deferredQuery}
            nextCursor={resources.data.pages.at(-1)?.next}
            loading={resources.isFetchingNextPage}
            error={resources.isFetchNextPageError}
            onLoadMore={() => void resources.fetchNextPage()}
            resourceLabel="search results"
          />
        ) : null}
      </CommandList>
    </CommandDialog>
  );
}

function SearchItem({
  id,
  label,
  detail,
  icon: Icon,
  onSelect,
}: {
  id: string;
  label: string;
  detail?: string;
  icon: typeof Search;
  onSelect: () => void;
}) {
  return (
    <CommandItem value={id} onSelect={onSelect} className="min-h-11 gap-3 px-3 py-2">
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate text-sm">{label}</span>
      {detail ? <span className="text-xs text-muted-foreground">{detail}</span> : null}
    </CommandItem>
  );
}

function resourceHref(base: string, result: ResourceSearchResult): string {
  if (result.kind === "app") return `${base}/apps/${encodeURIComponent(result.id)}`;
  if (result.kind === "workload")
    return `${base}/apps/${encodeURIComponent(result.app_id)}/workloads/${encodeURIComponent(result.name)}?kind=${encodeURIComponent(result.workload_kind)}`;
  if (result.kind === "task") return `${base}/tasks/${encodeURIComponent(result.id)}`;
  return `${base}/sandboxes/${encodeURIComponent(result.id)}`;
}
