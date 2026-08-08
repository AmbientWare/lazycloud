import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient, type Query, type QueryKey } from "@tanstack/react-query";

import { useEventStream } from "@/hooks/useEventStream";
import { withWorkspace } from "@/lib/api/client";
import { workspaceChangeEventSchema } from "@/lib/api/schemas";
import { accountQueryKeys, workspaceQueryKeys } from "@/lib/queries/workspace-keys";
import {
  WorkspaceLiveUpdatesContext,
  type WorkspaceLiveUpdatesContextValue,
} from "@/lib/workspace-context";
import {
  workspaceInvalidationTargets,
  type WorkspaceInvalidationTarget,
} from "./workspace-invalidations";

const ORDINARY_BATCH_MS = 150;
const EXPENSIVE_BATCH_MS = 2_000;
const RECOVERY_INTERVAL_MS = 60_000;

export function WorkspaceLiveUpdatesProvider({
  workspaceId,
  children,
}: {
  workspaceId: string;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const ordinaryTargets = useRef(new Map<string, QueryKey>());
  const expensiveTargets = useRef(new Map<string, QueryKey>());
  const ordinaryTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const expensiveTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [contractInvalid, setContractInvalid] = useState(false);

  const invalidateTarget = useCallback(
    (queryKey: QueryKey) =>
      queryClient.invalidateQueries({
        queryKey,
        refetchType: "active",
        predicate: (query) =>
          query.meta?.workspaceLiveEnabled === true &&
          (query.state.status !== "error" || query.meta.workspaceLiveRecoverErrors !== false),
      }),
    [queryClient],
  );

  const flush = useCallback(
    (targets: Map<string, QueryKey>) => {
      const queryKeys = [...targets.values()];
      targets.clear();
      for (const queryKey of queryKeys) void invalidateTarget(queryKey);
    },
    [invalidateTarget],
  );

  const enqueue = useCallback(
    (target: WorkspaceInvalidationTarget) => {
      const targets = target.expensive ? expensiveTargets.current : ordinaryTargets.current;
      targets.set(JSON.stringify(target.queryKey), target.queryKey);
      const timer = target.expensive ? expensiveTimer : ordinaryTimer;
      if (timer.current !== undefined) return;
      timer.current = setTimeout(
        () => {
          timer.current = undefined;
          flush(targets);
        },
        target.expensive ? EXPENSIVE_BATCH_MS : ORDINARY_BATCH_MS,
      );
    },
    [flush],
  );

  const reconcileCriticalQueries = useCallback(() => {
    const critical = (query: Query) =>
      query.meta?.workspaceLiveEnabled === true &&
      query.meta.workspaceLiveCritical === true &&
      (query.state.status !== "error" || query.meta.workspaceLiveRecoverErrors !== false);
    // Both roots: capacity is read per account now, so a stream that missed events
    // has to catch up records that no longer live under this workspace's key.
    for (const queryKey of [workspaceQueryKeys.root(workspaceId), accountQueryKeys.root()]) {
      void queryClient.invalidateQueries({
        queryKey,
        refetchType: "active",
        predicate: critical,
      });
    }
  }, [queryClient, workspaceId]);

  const onEvent = useCallback(
    (frame: { event: string; data: string }) => {
      if (frame.event !== "workspace.change") return;
      let payload: unknown;
      try {
        payload = JSON.parse(frame.data);
      } catch {
        setContractInvalid(true);
        return;
      }
      const parsed = workspaceChangeEventSchema.safeParse(payload);
      if (!parsed.success || parsed.data.workspace_id !== workspaceId) {
        setContractInvalid(true);
        return;
      }
      setContractInvalid(false);
      for (const target of workspaceInvalidationTargets(workspaceId, parsed.data)) {
        enqueue(target);
      }
    },
    [enqueue, workspaceId],
  );

  const streamStatus = useEventStream(withWorkspace("/api/v1/events/changes/stream", workspaceId), {
    onEvent,
    onOpen: reconcileCriticalQueries,
  });

  useEffect(() => {
    const timer = setInterval(reconcileCriticalQueries, RECOVERY_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [reconcileCriticalQueries]);

  useEffect(
    () => () => {
      if (ordinaryTimer.current !== undefined) clearTimeout(ordinaryTimer.current);
      if (expensiveTimer.current !== undefined) clearTimeout(expensiveTimer.current);
      ordinaryTargets.current.clear();
      expensiveTargets.current.clear();
    },
    [],
  );

  const value = useMemo<WorkspaceLiveUpdatesContextValue>(
    () => ({ status: contractInvalid ? "error" : streamStatus }),
    [contractInvalid, streamStatus],
  );

  return (
    <WorkspaceLiveUpdatesContext.Provider value={value}>
      {children}
    </WorkspaceLiveUpdatesContext.Provider>
  );
}
