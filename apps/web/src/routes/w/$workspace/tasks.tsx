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
import { countLabel } from "@/lib/format";
import { taskStatuses, workloadKinds } from "@/lib/api/schemas";
import { appSummariesQueryOptions } from "@/lib/queries/apps";
import {
  selectTaskList,
  taskMetricsQueryOptions,
  tasksInfiniteQueryOptions,
} from "@/lib/queries/tasks";
import { stubsQueryOptions } from "@/lib/queries/stubs";
import { useWorkspace } from "@/lib/workspace-context";

type TasksSearch = {
  status?: string;
  kind?: string;
  app?: string;
  workload?: string;
  deployment?: string;
};

export const Route = createFileRoute("/w/$workspace/tasks")({
  validateSearch: (search: Record<string, unknown>): TasksSearch => ({
    status: pickOption(search.status, taskStatuses),
    kind: pickOption(search.kind, workloadKinds),
    app: typeof search.app === "string" && search.app ? search.app : undefined,
    workload: typeof search.workload === "string" && search.workload ? search.workload : undefined,
    deployment:
      typeof search.deployment === "string" && search.deployment ? search.deployment : undefined,
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
    tasksInfiniteQueryOptions(workspace.id, {
      limit: 100,
      status: search.status,
      appId: search.app,
      stubIds: search.workload ? [search.workload] : undefined,
      deploymentId: search.deployment,
      kind: search.kind,
    }),
  );
  const apps = useQuery(appSummariesQueryOptions(workspace.id));
  const workloads = useQuery(stubsQueryOptions(workspace.id, search.app));
  const metrics = useQuery(taskMetricsQueryOptions(workspace.id));
  const taskList = selectTaskList(tasks.data, tasks.hasNextPage);
  const selectedDeployment = taskList.items.find((task) => task.deployment_id === search.deployment)
    ?.deployment?.name;

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
                label="Status"
                value={search.status}
                options={taskStatuses}
                allLabel="All statuses"
                onChange={(status) => setSearch({ status })}
              />
              <FilterSelect
                label="Type"
                value={search.kind}
                options={workloadKinds}
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
                onChange={(app) => setSearch({ app, workload: undefined })}
              />
              <FilterSelect
                label="Workload"
                value={search.workload}
                options={(workloads.data?.stubs ?? []).map((workload) => workload.id)}
                optionLabel={(stubId) =>
                  workloads.data?.stubs.find((workload) => workload.id === stubId)?.name ?? stubId
                }
                allLabel="All workloads"
                onChange={(workload) => setSearch({ workload })}
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
            </div>
          </div>
          {tasks.isError ? (
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
                  search.status || search.kind || search.app || search.workload || search.deployment
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
