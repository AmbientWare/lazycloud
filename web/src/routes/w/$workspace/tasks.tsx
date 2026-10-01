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
import { countLabel } from "@/lib/format";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import {
  selectTaskList,
  taskMetricsQueryOptions,
  tasksInfiniteQueryOptions,
} from "@/lib/queries/tasks";
import { deployedStubsQueryOptions } from "@/lib/queries/stubs";
import { useWorkspace } from "@/lib/workspace-context";

const TASK_STATUSES: readonly Schemas["TaskStatus"][] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];

/** Every task runs a function; endpoint and ASGI requests are listed on their workload. */
const TASK_KINDS = ["function"] as const;

type TasksSearch = {
  status?: Schemas["TaskStatus"];
  kind?: (typeof TASK_KINDS)[number];
  /** App and workload by name; a version narrows the workload. */
  app?: string;
  workload?: string;
  version?: number;
};

export const Route = createFileRoute("/w/$workspace/tasks")({
  validateSearch: (search: Record<string, unknown>): TasksSearch => ({
    status: pickOption(search.status, TASK_STATUSES),
    kind: pickOption(search.kind, TASK_KINDS),
    app: typeof search.app === "string" && search.app ? search.app : undefined,
    workload: typeof search.workload === "string" && search.workload ? search.workload : undefined,
    version:
      typeof search.version === "number" && Number.isInteger(search.version) && search.version > 0
        ? search.version
        : undefined,
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

  const tasks = useInfiniteQuery(
    tasksInfiniteQueryOptions(
      workspace.name,
      {
        status: search.status,
        app: search.app,
        function: search.workload,
        version: search.version,
      },
      100,
    ),
  );
  const apps = useQuery(appSummariesQueryOptions(workspace.id));
  const appNames = (apps.data?.items ?? []).map((item) => item.app.name);
  const appId = apps.data?.items.find((item) => item.app.name === search.app)?.app.id;
  const workloads = useQuery({
    ...deployedStubsQueryOptions(workspace.id, appId),
    enabled: !search.app || Boolean(appId),
  });
  const workloadNames = [...new Set((workloads.data?.stubs ?? []).map((stub) => stub.name))];
  const metrics = useQuery(taskMetricsQueryOptions(workspace.name));
  const taskList = selectTaskList(tasks.data, tasks.hasNextPage);

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
                countLabel(metrics.data.status_counts.failed, "failed", "failed"),
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
                label="Status"
                value={search.status}
                options={TASK_STATUSES}
                allLabel="All statuses"
                onChange={(status) => setSearch({ status })}
              />
              <FilterSelect
                label="Type"
                value={search.kind}
                options={TASK_KINDS}
                allLabel="All types"
                onChange={(kind) => setSearch({ kind })}
              />
              <FilterSelect
                label="App"
                value={search.app}
                options={appNames}
                allLabel="All apps"
                onChange={(app) => setSearch({ app, workload: undefined, version: undefined })}
              />
              <FilterSelect
                label="Workload"
                value={search.workload}
                options={workloadNames}
                allLabel="All workloads"
                onChange={(workload) => setSearch({ workload, version: undefined })}
              />
              {search.version ? (
                <div className="flex shrink-0 items-center gap-2 border-l border-border pl-3 text-xs text-muted-foreground">
                  <span>Version {search.version}</span>
                  <button
                    type="button"
                    className="text-brand hover:underline"
                    onClick={() => setSearch({ version: undefined })}
                  >
                    Clear
                  </button>
                </div>
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
                emptyMessage={
                  search.status || search.app || search.workload || search.version
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

function FilterSelect<T extends string>({
  label,
  value,
  options,
  allLabel,
  onChange,
}: {
  label: string;
  value: T | undefined;
  options: readonly T[];
  allLabel: string;
  onChange: (value: T | undefined) => void;
}) {
  return (
    <Select
      value={value ?? "all"}
      onValueChange={(next) => onChange(next === "all" ? undefined : (next as T))}
    >
      <SelectTrigger aria-label={label} className="h-7 w-36 text-xs">
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value="all">{allLabel}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option} value={option}>
            {option}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
