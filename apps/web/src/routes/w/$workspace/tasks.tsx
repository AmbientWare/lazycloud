import { useState } from "react";
import { createFileRoute, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Pause, Play, Search, X } from "lucide-react";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { TaskTable } from "@/components/shared/TaskTable";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { stubKinds, taskStatuses } from "@/lib/api/schemas";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import { selectTaskList, tasksInfiniteQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

type TasksSearch = {
  status?: string;
  kind?: string;
  app?: string;
  workload?: string;
  deployment?: string;
  q?: string;
  root?: boolean;
};

export const Route = createFileRoute("/w/$workspace/tasks")({
  validateSearch: (search: Record<string, unknown>): TasksSearch => ({
    status: pickOption(search.status, taskStatuses),
    kind: pickOption(search.kind, stubKinds),
    app: typeof search.app === "string" && search.app ? search.app : undefined,
    workload:
      typeof search.workload === "string" && search.workload ? search.workload : undefined,
    deployment:
      typeof search.deployment === "string" && search.deployment ? search.deployment : undefined,
    q: typeof search.q === "string" && search.q.trim() ? search.q.trim() : undefined,
    root: search.root === true || search.root === "true" ? true : undefined,
  }),
  component: TasksPage,
  errorComponent: RouteErrorFallback,
});

function pickOption<T extends string>(value: unknown, options: readonly T[]): T | undefined {
  return typeof value === "string" && (options as readonly string[]).includes(value)
    ? (value as T)
    : undefined;
}

/** Workspace-wide tasks feed; the task drawer nests over it at /tasks/$taskId. */
function TasksPage() {
  const { workspace } = useWorkspace();
  const search = Route.useSearch();
  const navigate = Route.useNavigate();
  const [live, setLive] = useState(true);

  const tasks = useInfiniteQuery(
    tasksInfiniteQueryOptions(workspace.id, {
      limit: 100,
      status: search.status,
      appId: search.app,
      stubIds: search.workload ? [search.workload] : undefined,
      deploymentId: search.deployment,
      kind: search.kind,
      search: search.q,
      rootOnly: search.root,
      live,
    }),
  );
  const apps = useQuery(appSummariesQueryOptions(workspace.id));
  const taskList = selectTaskList(tasks.data, tasks.hasNextPage);
  const selectedDeployment = taskList.items.find(
    (task) => task.deployment_id === search.deployment,
  )?.deployment?.name;
  const selectedWorkload = taskList.items.find(
    (task) => task.stub_id === search.workload,
  )?.workload?.name;

  const setSearch = (patch: Partial<TasksSearch>) => {
    void navigate({ search: (previous: TasksSearch) => ({ ...previous, ...patch }) });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkspacePage
        title="Tasks"
        actions={
          <Button
            variant="outline"
            size="sm"
            aria-label={live ? "Pause task updates" : "Resume task updates"}
            title={live ? "Pause updates" : "Resume updates"}
            onClick={() => setLive((value) => !value)}
          >
            {live ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
            {live ? "Pause" : "Resume"}
          </Button>
        }
        contentClassName="mx-auto w-full max-w-[1600px]"
      >
        <section
          className="panel flex h-full min-h-0 flex-col overflow-hidden rounded-md"
          aria-label="Tasks"
        >
          <div
            data-tasks-toolbar=""
            className="flex min-h-12 shrink-0 items-center gap-3 overflow-x-auto border-b border-border px-3 py-2"
          >
            <form
              className="flex h-8 w-64 shrink-0 items-center gap-2 rounded-md border border-border bg-background/60 px-2.5"
              onSubmit={(event) => {
                event.preventDefault();
                const form = new FormData(event.currentTarget);
                const query = String(form.get("q") ?? "").trim();
                setSearch({ q: query || undefined });
              }}
            >
              <Search className="size-3.5 shrink-0 text-muted-foreground" />
              <input
                key={search.q ?? ""}
                name="q"
                type="search"
                defaultValue={search.q}
                aria-label="Search tasks"
                placeholder="Search name or ID"
                className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
              />
              {search.q ? (
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  aria-label="Clear task search"
                  title="Clear search"
                  className="size-6"
                  onClick={() => setSearch({ q: undefined })}
                >
                  <X className="size-3.5" />
                </Button>
              ) : null}
            </form>
            <div data-tasks-filters="" className="ml-auto flex shrink-0 items-center gap-2">
              <label className="flex h-7 shrink-0 items-center gap-2 px-1 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={search.root ?? false}
                  onChange={(event) => setSearch({ root: event.target.checked || undefined })}
                />
                Root tasks
              </label>
              <FilterSelect
                label="Status"
                value={search.status}
                options={taskStatuses}
                allLabel="All statuses"
                onChange={(status) => setSearch({ status })}
              />
              <FilterSelect
                label="Type"
                value={search.kind}
                options={stubKinds}
                allLabel="All types"
                onChange={(kind) => setSearch({ kind })}
              />
              <FilterSelect
                label="App"
                value={search.app}
                options={(apps.data?.items ?? []).map((item) => item.app.id)}
                optionLabel={(appId) =>
                  apps.data?.items.find((item) => item.app.id === appId)?.app.name ?? appId
                }
                allLabel="All apps"
                onChange={(app) => setSearch({ app })}
              />
              {search.deployment ? (
                <div className="flex shrink-0 items-center gap-2 border-l border-border pl-3 text-xs text-muted-foreground">
                  <span>Version {selectedDeployment ?? search.deployment}</span>
                  <button
                    type="button"
                    className="text-brand hover:underline"
                    onClick={() => setSearch({ deployment: undefined })}
                  >
                    Clear
                  </button>
                </div>
              ) : null}
              {search.workload ? (
                <div className="flex shrink-0 items-center gap-2 border-l border-border pl-3 text-xs text-muted-foreground">
                  <span>Workload {selectedWorkload ?? "filter"}</span>
                  <button
                    type="button"
                    className="text-brand hover:underline"
                    onClick={() => setSearch({ workload: undefined })}
                  >
                    Clear
                  </button>
                </div>
              ) : null}
            </div>
          </div>
          {tasks.isError ? (
            <div className="flex min-h-0 flex-1 items-center justify-center p-4 text-sm text-destructive">
              {tasks.error.message}
            </div>
          ) : (
            <>
              <TaskTable
                tasks={tasks.isPending ? undefined : taskList.items}
                taskLink={(taskId) => ({
                  to: "/w/$workspace/tasks/$taskId",
                  params: { workspace: workspace.name, taskId },
                  search,
                })}
                emptyMessage={
                  search.status ||
                  search.kind ||
                  search.app ||
                  search.workload ||
                  search.deployment ||
                  search.q ||
                  search.root
                    ? "No tasks match these filters"
                    : "No tasks yet"
                }
                className="min-h-0 flex-1"
                continuation={
                  <InfiniteScrollBoundary
                    nextCursor={taskList.nextCursor}
                    loading={tasks.isFetchingNextPage}
                    error={tasks.isFetchNextPageError}
                    onLoadMore={() => void tasks.fetchNextPage()}
                    resourceLabel="tasks"
                  />
                }
              />
            </>
          )}
        </section>
      </WorkspacePage>

      <Outlet />
    </div>
  );
}

function FilterSelect({
  label,
  value,
  options,
  optionLabel,
  allLabel,
  onChange,
}: {
  label: string;
  value: string | undefined;
  options: readonly string[];
  optionLabel?: (value: string) => string;
  allLabel: string;
  onChange: (value: string | undefined) => void;
}) {
  return (
    <Select
      value={value ?? "all"}
      onValueChange={(next) => onChange(next === "all" ? undefined : next)}
    >
      <SelectTrigger aria-label={label} className="h-7 w-36 text-xs">
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value="all">{allLabel}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option} value={option}>
            {optionLabel ? optionLabel(option) : option}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
