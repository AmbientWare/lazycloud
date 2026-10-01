import { Link, useNavigate, type LinkProps } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban, Loader2, RotateCcw } from "lucide-react";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { PanelErrorBoundary } from "@/components/shared/ErrorBoundary";
import { LinearTab, LinearTabsList } from "@/components/shared/LinearSelect";
import { LiveDuration, LiveRelativeTime } from "@/components/shared/LiveTime";
import { DrawerHeader, DrawerHeaderSkeleton } from "@/components/shared/DrawerHeader";
import { StatusChip } from "@/components/shared/StatusChip";
import { TaskPendingNotice } from "@/components/shared/TaskPendingNotice";
import { StubKindIcon } from "@/components/shared/StubKindIcon";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import type { Schemas } from "@/lib/api/client";
import { startupBetween } from "@/lib/format";
import {
  cancelTask,
  isRequest,
  requestQueryOptions,
  rerunTask,
  rowFacts,
  taskFinished,
  taskQueryOptions,
  type TaskRow,
} from "@/lib/queries/tasks";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";
import { useWorkspace } from "@/lib/workspace-context";

import { Artifacts } from "@/components/shared/Artifacts";
import { ContainerTab } from "@/components/shared/TaskDrawer/ContainerTab";
import { LogViewer } from "@/components/shared/TaskDrawer/LogViewer";
import { failureText, ResultBody } from "@/components/shared/TaskDrawer/ResultBody";
import { TaskTimeline } from "@/components/shared/TaskDrawer/TaskTimeline";
import { PhaseBar } from "@/components/shared/TaskDrawer/TaskTimeline/PhaseBar";
import { StatCell } from "@/components/shared/TaskDrawer/StatCell";

/**
 * Slide-over task detail; rendered by nested routes over app and deployment
 * pages. An endpoint's or ASGI app's row opens its request record instead.
 */
export function TaskDrawer({
  taskId,
  request = false,
  taskLink,
  onClose,
}: {
  taskId: string;
  /** The id names an endpoint or ASGI request record rather than a task. */
  request?: boolean;
  /** Builds the drawer route for related tasks (timeline rows) in this page context. */
  taskLink: (taskId: string) => Pick<LinkProps, "to" | "params" | "search">;
  onClose: () => void;
}) {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const task = useQuery({ ...taskQueryOptions(workspace.name, taskId), enabled: !request });
  const record = useQuery({ ...requestQueryOptions(workspace.name, taskId), enabled: request });
  const read = request ? record : task;
  const row: TaskRow | undefined = request ? record.data : task.data?.task;

  const cancel = useMutation({
    mutationFn: () => cancelTask(workspace.name, taskId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.tasks.detail(workspace.name, taskId),
      });
    },
  });

  const rerun = useMutation({
    mutationFn: () => rerunTask(workspace.name, taskId),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({
        queryKey: workspaceQueryKeys.tasks.lists(workspace.name),
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
        {read.isPending && !row ? (
          <TaskDrawerSkeleton />
        ) : !row ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <SheetTitle className="sr-only">Task</SheetTitle>
            <div className="flex min-h-0 flex-1 items-center justify-center p-4">
              <ApiErrorNotice
                error={read.error ?? new Error("Task could not be loaded")}
                title="Task could not be loaded"
                onRetry={() => void read.refetch()}
                retrying={read.isFetching}
                className="panel w-full max-w-lg rounded-md"
              />
            </div>
          </div>
        ) : (
          <TaskDrawerBody
            key={row.id}
            row={row}
            result={task.data?.result ?? null}
            workspace={workspace.name}
            taskLink={taskLink}
            onCancel={() => cancel.mutate()}
            cancelPending={cancel.isPending}
            cancelError={cancel.isError ? cancel.error.message : null}
            onRerun={() => rerun.mutate()}
            rerunPending={rerun.isPending}
            rerunError={rerun.isError ? rerun.error.message : null}
            refreshError={read.isError ? read.error : null}
            refreshPending={read.isFetching}
            onRefresh={() => void read.refetch()}
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
  row,
  result,
  workspace,
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
  row: TaskRow;
  result: Schemas["Payload"] | null;
  workspace: string;
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
  const workspaceId = useWorkspace().workspace.id;
  const facts = rowFacts(row);
  const task = isRequest(row) ? null : row;
  const finished = taskFinished(facts.status);
  const error = task?.failure
    ? failureText(task.failure)
    : isRequest(row) && row.status >= 500
      ? `${row.method} ${row.path} answered ${row.status}`
      : null;

  return (
    <div className="content-transition flex min-h-0 flex-1 flex-col">
      <DrawerHeader>
        <div className="flex min-w-0 flex-wrap items-center gap-2.5">
          <SheetTitle className="min-w-0 truncate">{facts.name}</SheetTitle>
          <span aria-live="polite">
            <StatusChip status={facts.status} live={facts.status === "running"} />
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-2 max-sm:w-full max-sm:justify-end">
            {task && finished ? (
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
            {task && !finished ? (
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
      {task ? <TaskPendingNotice task={task} /> : null}

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
            <span className="flex items-center gap-1.5">
              <StubKindIcon kind={facts.kind} className="size-3" />
              <Link
                to="/w/$workspace/apps/$app/workloads/$kind/$name"
                params={{ workspace, app: row.app, kind: facts.kind, name: facts.name }}
                className="text-brand hover:underline"
              >
                {facts.name}
              </Link>
              <span>{facts.kind}</span>
            </span>
            <Link
              to="/w/$workspace/apps/$app"
              params={{ workspace, app: row.app }}
              className="text-brand hover:underline"
            >
              {row.app}
            </Link>
            {row.version ? <span>v{row.version}</span> : null}
            <span>
              Requested <LiveRelativeTime value={facts.createdAt} />
            </span>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4">
            <StatCell
              label="Queued"
              value={startupBetween(facts.createdAt, facts.startedAt) ?? "—"}
              className="border-b border-r border-border sm:border-b-0"
            />
            <StatCell
              label="Execution"
              value={<LiveDuration startedAt={facts.startedAt} finishedAt={facts.finishedAt} />}
              className="border-b border-border sm:border-b-0 sm:border-r"
            />
            <StatCell
              label="Total"
              value={<LiveDuration startedAt={facts.createdAt} finishedAt={facts.finishedAt} />}
              className="border-r border-border"
            />
            <StatCell
              label="Attempt"
              value={task ? `${Math.max(task.attempts, 1)}/${task.max_attempts}` : "1/1"}
            />
          </div>

          <div className="border-t border-border">
            <div className="micro-label px-3 pt-2.5">Lifecycle</div>
            <PhaseBar
              workspace={workspace}
              containerId={row.container_id}
              createdAt={facts.createdAt}
              startedAt={facts.startedAt}
              finishedAt={facts.finishedAt}
              live={!finished}
            />
          </div>
        </section>

        <Tabs
          data-task-inspector=""
          defaultValue={error || result ? "result" : "logs"}
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
            <PanelErrorBoundary key={row.id} title="Logs could not be displayed">
              <LogViewer
                workspace={workspace}
                source={task ? { task: row.id } : { request: row.id }}
                className="h-full min-h-0"
              />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="result" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={row.id} title="Result could not be displayed">
              <ResultBody error={error} result={result} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="artifacts" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={row.id} title="Artifacts could not be displayed">
              <Artifacts key={`${workspace}/${row.id}`} workspaceId={workspaceId} taskId={row.id} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="trace" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={row.id} title="Trace could not be displayed">
              <TaskTimeline workspace={workspace} taskLink={taskLink} row={row} />
            </PanelErrorBoundary>
          </TabsContent>
          <TabsContent value="container" className="m-0 min-h-0 flex-1 overflow-auto">
            <PanelErrorBoundary key={row.id} title="Container details could not be displayed">
              <ContainerTab
                workspace={workspace}
                containerId={row.container_id}
                waiting={!finished}
                live={!finished}
              />
            </PanelErrorBoundary>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
