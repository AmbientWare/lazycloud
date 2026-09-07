import { Link, useNavigate, type LinkProps } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Loader2, RotateCcw } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { LiveDuration, LiveRelativeTime } from "@/components/shared/LiveTime";
import { ShellButton } from "@/components/shared/ShellDialog";
import { DrawerHeader, DrawerHeaderSkeleton } from "@/components/shared/DrawerHeader";
import { StatusChip } from "@/components/shared/StatusChip";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { postJson, withWorkspace } from "@/lib/api/client";
import { isTerminalTaskStatus, taskSchema, type Task } from "@/lib/api/schemas";
import { startupBetween } from "@/lib/format";
import { rerunTask, taskQueryOptions } from "@/lib/queries/tasks";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { ArtifactsTab } from "@/components/shared/TaskDrawer/ArtifactsTab";
import { ContainerTab } from "@/components/shared/TaskDrawer/ContainerTab";
import { LogViewer } from "@/components/shared/TaskDrawer/LogViewer";
import { ResultBody } from "@/components/shared/TaskDrawer/ResultBody";
import { TaskTimeline } from "@/components/shared/TaskDrawer/TaskTimeline";
import { PhaseBar } from "@/components/shared/TaskDrawer/TaskTimeline/PhaseBar";
import { StatCell } from "@/components/shared/TaskDrawer/StatCell";

/** Slide-over task detail; rendered by nested routes over app and deployment pages. */
export function TaskDrawer({
  taskId,
  taskLink,
  onClose,
}: {
  taskId: string;
  /** Builds the drawer route for related tasks (timeline rows) in this page context. */
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  onClose: () => void;
}) {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const task = useQuery(taskQueryOptions(workspace.id, taskId));

  const cancel = useMutation({
    mutationFn: () =>
      postJson(withWorkspace(`/api/v1/tasks/${taskId}/cancel`, workspace.id), taskSchema),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.tasks.detail(workspace.id, taskId),
      });
    },
  });

  const rerun = useMutation({
    mutationFn: () => rerunTask(workspace.id, taskId),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.tasks.lists(workspace.id),
      });
      // Move the drawer to the freshly submitted task in the same page context.
      void navigate(taskLink(created.id));
    },
  });

  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent
        aria-describedby={undefined}
        aria-label="Task detail"
        className="gap-0 bg-background max-sm:left-0 max-sm:right-0 max-sm:max-w-none max-sm:border-l-0 sm:max-w-3xl xl:max-w-4xl"
      >
        {task.isPending && !task.data ? (
          <TaskDrawerSkeleton />
        ) : !task.data ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <SheetTitle className="sr-only">Task</SheetTitle>
            <div className="flex min-h-0 flex-1 items-center justify-center p-4">
              <ApiErrorNotice
                error={task.error ?? new Error("Task could not be loaded")}
                title="Task could not be loaded"
                onRetry={() => void task.refetch()}
                retrying={task.isFetching}
                className="panel w-full max-w-lg rounded-md"
              />
            </div>
          </div>
        ) : (
          <TaskDrawerBody
            key={task.data.id}
            record={task.data}
            terminal={isTerminalTaskStatus(task.data.status)}
            workspace={workspace}
            taskId={taskId}
            taskLink={taskLink}
            onCancel={() => cancel.mutate()}
            cancelPending={cancel.isPending}
            cancelError={cancel.isError ? cancel.error.message : null}
            onRerun={() => rerun.mutate()}
            rerunPending={rerun.isPending}
            rerunError={rerun.isError ? rerun.error.message : null}
            refreshError={task.isError ? task.error : null}
            refreshPending={task.isFetching}
            onRefresh={() => void task.refetch()}
          />
        )}
      </SheetContent>
    </Sheet>
  );
}

/** Layout-matched placeholder while the task record loads. */
function TaskDrawerSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col" aria-hidden="true">
      <SheetTitle className="sr-only">Task</SheetTitle>
      <DrawerHeaderSkeleton>
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-5 w-16" />
      </DrawerHeaderSkeleton>
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3">
        <div className="panel shrink-0 overflow-hidden rounded-md">
          <div className="flex items-center gap-3 border-b border-border px-4 py-2">
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-4 w-56" />
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-4">
            {Array.from({ length: 4 }, (_, index) => (
              <div
                key={index}
                className={cn(
                  "space-y-2 p-3",
                  index % 2 === 0 && "border-r border-border",
                  index < 2 && "border-b border-border sm:border-b-0",
                  index === 1 && "sm:border-r sm:border-border",
                  index === 2 && "sm:border-r sm:border-border",
                )}
              >
                <Skeleton className="h-3 w-14" />
                <Skeleton className="h-4 w-16" />
              </div>
            ))}
          </div>
          <div className="space-y-2 border-t border-border p-3">
            <Skeleton className="h-3 w-16" />
            <Skeleton className="h-24 w-full" />
          </div>
        </div>
        <div className="panel min-h-0 flex-1 space-y-3 overflow-hidden rounded-md p-3">
          <Skeleton className="h-6 w-64" />
          <Skeleton className="h-full min-h-40 w-full" />
        </div>
      </div>
    </div>
  );
}

function TaskDrawerBody({
  record,
  terminal,
  workspace,
  taskId,
  taskLink,
  onCancel,
  cancelPending,
  cancelError,
  onRerun,
  rerunPending,
  rerunError,
  refreshError,
  refreshPending,
  onRefresh,
}: {
  record: Task;
  terminal: boolean;
  workspace: { id: string; name: string };
  taskId: string;
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  onCancel: () => void;
  cancelPending: boolean;
  cancelError: string | null;
  onRerun: () => void;
  rerunPending: boolean;
  rerunError: string | null;
  refreshError: Error | null;
  refreshPending: boolean;
  onRefresh: () => void;
}) {
  const kind = record.workload?.kind;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <DrawerHeader>
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <SheetTitle className="min-w-0 truncate">{record.name}</SheetTitle>
          <span aria-live="polite">
            <StatusChip status={record.status} live={record.status === "running"} />
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2 max-sm:w-full max-sm:justify-end">
            {record.actions.can_shell && record.container_id ? (
              <ShellButton containerId={record.container_id} running />
            ) : null}
            {record.actions.can_rerun ? (
              <Button
                variant="outline"
                size="sm"
                disabled={rerunPending}
                aria-busy={rerunPending}
                onClick={onRerun}
              >
                {rerunPending ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <RotateCcw className="size-3" />
                )}
                {rerunPending ? "Starting" : "Re-run"}
              </Button>
            ) : null}
            {record.actions.can_cancel ? (
              <Button
                variant="destructive"
                size="sm"
                disabled={cancelPending}
                aria-busy={cancelPending}
                onClick={onCancel}
              >
                {cancelPending ? (
                  <Loader2 className="size-3 animate-spin" />
                ) : (
                  <Ban className="size-3" />
                )}
                {cancelPending ? "Cancelling" : "Cancel"}
              </Button>
            ) : null}
          </div>
        </div>
        {cancelError || rerunError ? (
          <p className="mt-1.5 text-xs text-destructive">{cancelError ?? rerunError}</p>
        ) : null}
      </DrawerHeader>

      {refreshError ? (
        <ApiErrorNotice
          error={refreshError}
          title="Task updates interrupted"
          onRetry={onRefresh}
          retrying={refreshPending}
          compact
          className="shrink-0 border-b border-warning/30 bg-warning/5"
        />
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-3">
        <section
          data-task-summary=""
          className="panel shrink-0 overflow-hidden rounded-md"
          aria-label="Task summary"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-2 text-xs text-muted-foreground">
            {record.workload && kind ? (
              <span className="flex items-center gap-1.5">
                <StubKindIcon kind={kind} className="size-3" />
                {record.app_id ? (
                  <Link
                    to="/w/$workspace/apps/$appId/workloads/$name"
                    params={{
                      workspace: workspace.name,
                      appId: record.app_id,
                      name: record.workload.name,
                    }}
                    className="text-brand hover:underline"
                  >
                    {record.workload.name}
                  </Link>
                ) : (
                  record.workload.name
                )}
                <span>{kind}</span>
              </span>
            ) : record.handler ? (
              <span className="mono">{record.handler}</span>
            ) : null}
            {record.app_id ? (
              <Link
                to="/w/$workspace/apps/$appId"
                params={{ workspace: workspace.name, appId: record.app_id }}
                className="text-brand hover:underline"
              >
                {record.app?.name ?? "App"}
              </Link>
            ) : null}
            {record.deployment ? <span>v{record.deployment.version}</span> : null}
            <span>
              Requested <LiveRelativeTime value={record.created_at} />
            </span>
            {record.exit_code !== null && record.exit_code !== undefined ? (
              <span>Exit {record.exit_code}</span>
            ) : null}
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4">
            <StatCell
              label="Queued"
              value={startupBetween(record.created_at, record.started_at) ?? "—"}
              className="border-b border-r border-border sm:border-b-0"
            />
            <StatCell
              label="Execution"
              value={<LiveDuration startedAt={record.started_at} finishedAt={record.finished_at} />}
              className="border-b border-border sm:border-b-0 sm:border-r"
            />
            <StatCell
              label="Total"
              value={<LiveDuration startedAt={record.created_at} finishedAt={record.finished_at} />}
              className="border-r border-border"
            />
            <StatCell
              label="Attempt"
              value={`${Math.max(record.attempt_number, 1)}/${record.max_attempts}`}
            />
          </div>

          <div className="border-t border-border">
            <div className="micro-label px-3 pt-2.5">Lifecycle</div>
            <PhaseBar workspaceId={workspace.id} task={record} />
          </div>
        </section>

        <Tabs
          data-task-inspector=""
          defaultValue={
            record.error || (record.result !== null && record.result !== undefined)
              ? "result"
              : "logs"
          }
          className="panel flex min-h-0 flex-1 flex-col overflow-hidden rounded-md"
        >
          <LinearTabsList ariaLabel="Task inspector views" className="shrink-0 bg-card px-2">
            <LinearTab value="logs">Logs</LinearTab>
            <LinearTab value="result">Result</LinearTab>
            <LinearTab value="artifacts">Artifacts</LinearTab>
            <LinearTab value="trace">Trace</LinearTab>
            <LinearTab value="container">Container</LinearTab>
          </LinearTabsList>
          <TabsContent value="logs" className="m-0 min-h-0 flex-1 overflow-hidden">
            <PanelErrorBoundary key={taskId} title="Logs could not be displayed">
              <LogViewer
                workspaceId={workspace.id}
                scope={{ taskId, stubId: record.stub_id ?? undefined }}
                className="h-full min-h-0"
              />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="result" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={taskId} title="Result could not be displayed">
              <ResultBody error={record.error} result={record.result} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="artifacts" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={taskId} title="Artifacts could not be displayed">
              <ArtifactsTab workspaceId={workspace.id} taskId={taskId} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="trace" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={taskId} title="Trace could not be displayed">
              <TaskTimeline workspaceId={workspace.id} taskLink={taskLink} task={record} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="container" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={taskId} title="Container details could not be displayed">
              <ContainerTab record={record} workspaceId={workspace.id} live={!terminal} />
            </PanelErrorBoundary>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
