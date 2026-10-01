import { createFileRoute, Outlet } from "@tanstack/react-router";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { RouteErrorFallback } from "@/components/shared/ErrorBoundary";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { PanelError } from "@/components/shared/PanelError";
import { TaskTable } from "@/components/shared/TaskTable";
import { WorkspacePage } from "@/components/shared/WorkspacePage";
import { PageFacts } from "@/components/shared/WorkspacePage/PageFacts";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { Schemas } from "@/lib/api/client";
import { countLabel, formatKind } from "@/lib/format";
import { appsQueryOptions, selectApps } from "@/lib/queries/apps";
import { deploymentsQueryOptions, selectDeployments } from "@/lib/queries/deployments";
import { taskMetricsQueryOptions } from "@/lib/queries/metrics";
import { selectTaskList, tasksInfiniteQueryOptions } from "@/lib/queries/tasks";
import { useWorkspace } from "@/lib/workspace-context";

const TASK_STATUSES: readonly Schemas["TaskStatus"][] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];
const WORKLOAD_KINDS: readonly Schemas["WorkloadKind"][] = ["function"];

type TasksSearch = {
  status?: Schemas["TaskStatus"];
  kind?: Schemas["WorkloadKind"];
  /** App name. */
  app?: string;
  /** Workload name within `app`. */
  workload?: string;
};

export const Route = createFileRoute("/w/$workspace/tasks")({
  validateSearch: (search: Record<string, unknown>): TasksSearch => ({
    status: pickOption(search.status, TASK_STATUSES),
    kind: pickOption(search.kind, WORKLOAD_KINDS),
    app: typeof search.app === "string" && search.app ? search.app : undefined,
    workload: typeof search.workload === "string" && search.workload ? search.workload : undefined,
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

  // Every task belongs to a function, the only workload kind with tasks, so
  // the type filter narrows nothing further on the server.
  const tasks = useInfiniteQuery(
    tasksInfiniteQueryOptions(workspace.name, {
      limit: 100,
      status: search.status,
      app: search.app,
      function: search.app ? search.workload : undefined,
    }),
  );
  const apps = useInfiniteQuery(appsQueryOptions(workspace.name));
  const workloads = useInfiniteQuery({
    ...deploymentsQueryOptions(workspace.name, { app: search.app }),
    enabled: Boolean(search.app),
  });
  const metrics = useQuery(taskMetricsQueryOptions(workspace.name));
  const taskList = selectTaskList(tasks.data, tasks.hasNextPage);
  const appNames = selectApps(apps.data, false).items.map((app) => app.name);
  const workloadNames = selectDeployments(workloads.data, false).items.map((item) => item.name);
  const filtered = Boolean(search.status || search.kind || search.app || search.workload);

  const setSearch = (patch: Partial<TasksSearch>) => {
    void navigate({ search: (previous: TasksSearch) => ({ ...previous, ...patch }) });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WorkspacePage
        title="Tasks"
        description={
          metrics.data ? (
            <PageFacts
              items={[
                countLabel(metrics.data.total, "task"),
                countLabel(metrics.data.failed, "failed", "failed"),
                "last 24 hours",
              ]}
            />
          ) : null
        }
      >
        <section
          className="panel flex h-full min-h-0 flex-col overflow-hidden rounded-md"
          aria-label="Tasks"
        >
          <div
            data-tasks-toolbar=""
            className="flex min-h-12 shrink-0 items-center gap-3 overflow-x-auto border-b border-border px-3 py-2"
          >
            <div data-tasks-filters="" className="flex shrink-0 items-center gap-2">
              <FilterSelect
                label="App"
                value={search.app}
                options={appNames}
                allLabel="All apps"
                onChange={(app) => setSearch({ app, workload: undefined })}
              />
              <FilterSelect
                label="Status"
                value={search.status}
                options={TASK_STATUSES}
                optionLabel={formatKind}
                allLabel="All statuses"
                onChange={(status) => setSearch({ status: pickOption(status, TASK_STATUSES) })}
              />
              <FilterSelect
                label="Type"
                value={search.kind}
                options={WORKLOAD_KINDS}
                optionLabel={formatKind}
                allLabel="All types"
                onChange={(kind) => setSearch({ kind: pickOption(kind, WORKLOAD_KINDS) })}
              />
              <FilterSelect
                label="Workload"
                value={search.workload}
                options={workloadNames}
                allLabel={search.app ? "All workloads" : "Choose an app"}
                disabled={!search.app}
                onChange={(workload) => setSearch({ workload })}
              />
              {filtered ? (
                <button
                  type="button"
                  className="shrink-0 border-l border-border pl-3 text-xs text-brand hover:underline"
                  onClick={() =>
                    setSearch({
                      status: undefined,
                      kind: undefined,
                      app: undefined,
                      workload: undefined,
                    })
                  }
                >
                  Clear
                </button>
              ) : null}
            </div>
          </div>
          {tasks.isError && !tasks.isFetchNextPageError ? (
            <PanelError message={tasks.error.message} layout="centered" />
          ) : (
            <>
              <TaskTable
                tasks={tasks.isPending ? undefined : taskList.items}
                taskLink={(taskId) => ({
                  to: "/w/$workspace/tasks/$taskId",
                  params: { workspace: workspace.name, taskId },
                  search,
                })}
                emptyMessage={filtered ? "No tasks match these filters" : "No tasks yet"}
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
  disabled = false,
  onChange,
}: {
  label: string;
  value: string | undefined;
  options: readonly string[];
  optionLabel?: (value: string) => string;
  allLabel: string;
  disabled?: boolean;
  onChange: (value: string | undefined) => void;
}) {
  return (
    <Select
      value={value ?? "all"}
      disabled={disabled}
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
