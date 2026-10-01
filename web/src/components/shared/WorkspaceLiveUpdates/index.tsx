import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { focusManager, useQueryClient, type Query, type QueryKey } from "@tanstack/react-query";

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

/**
 * Floor on how often one expensive key may be refetched.
 *
 * The aggregates behind these keys cost the server half a second or more each
 * and answer over a 24-hour window, so a busy workspace publishing tens of
 * changes a minute would otherwise spend most of a core keeping a number that
 * moves in the third decimal place up to date. Batching alone does not bound
 * that: a steady stream refills the batch as fast as it drains.
 */
const EXPENSIVE_MIN_INTERVAL_MS = 15_000;

/**
 * Backstop sweep for changes the server never published.
 *
 * A reconnect replays what it missed, but `WorkspaceChangeService.publish`
 * logs and drops the event when Redis is unavailable, and nothing replays a
 * change that was never written. This sweep is the only recovery from that, so
 * it is paced for a rare dropped publish rather than for freshness, which the
 * stream already owns.
 */
const RECOVERY_INTERVAL_MS = 300_000;

type ExpensiveTarget = {
  queryKey: QueryKey;
  invalidatedAt: number;
  timer: ReturnType<typeof setTimeout> | undefined;
};

export function WorkspaceLiveUpdatesProvider({
  workspaceId,
  children,
}: {
  workspaceId: string;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const ordinaryTargets = useRef(new Map<string, QueryKey>());
  const ordinaryTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const expensiveTargets = useRef(new Map<string, ExpensiveTarget>());
  const resumable = useRef(false);
  const [contractInvalid, setContractInvalid] = useState(false);

  const visible = useSyncExternalStore(subscribeToVisibility, isVisible, alwaysVisible);
  const streamUrl = withWorkspace("/api/v1/events/changes/stream", workspaceId);

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

  const flushOrdinary = useCallback(() => {
    const queryKeys = [...ordinaryTargets.current.values()];
    ordinaryTargets.current.clear();
    for (const queryKey of queryKeys) void invalidateTarget(queryKey);
  }, [invalidateTarget]);

  const enqueue = useCallback(
    (target: WorkspaceInvalidationTarget) => {
      const id = JSON.stringify(target.queryKey);
      if (!target.expensive) {
        ordinaryTargets.current.set(id, target.queryKey);
        if (ordinaryTimer.current !== undefined) return;
        ordinaryTimer.current = setTimeout(() => {
          ordinaryTimer.current = undefined;
          flushOrdinary();
        }, ORDINARY_BATCH_MS);
        return;
      }

      const now = Date.now();
      const pending = expensiveTargets.current.get(id);
      if (pending === undefined) {
        expensiveTargets.current.set(id, {
          queryKey: target.queryKey,
          invalidatedAt: now,
          timer: undefined,
        });
        void invalidateTarget(target.queryKey);
        return;
      }
      if (pending.timer !== undefined) return;
      const waitMs = EXPENSIVE_MIN_INTERVAL_MS - (now - pending.invalidatedAt);
      if (waitMs <= 0) {
        pending.invalidatedAt = now;
        void invalidateTarget(pending.queryKey);
        return;
      }
      pending.timer = setTimeout(() => {
        pending.timer = undefined;
        pending.invalidatedAt = Date.now();
        void invalidateTarget(pending.queryKey);
      }, waitMs);
    },
    [flushOrdinary, invalidateTarget],
  );

  const reconcileCriticalQueries = useCallback(() => {
    const critical = (query: Query) =>
      query.meta?.workspaceLiveEnabled === true &&
      query.meta.workspaceLiveCritical === true &&
      (query.state.status !== "error" || query.meta.workspaceLiveRecoverErrors !== false);
    // Both roots: capacity is keyed by account, so a stream that missed events has
    // to catch up records that do not live under this workspace's key.
    for (const queryKey of [workspaceQueryKeys.root(workspaceId), accountQueryKeys.root()]) {
      void queryClient.invalidateQueries({
        queryKey,
        refetchType: "active",
        predicate: critical,
      });
    }
  }, [queryClient, workspaceId]);

  const onEvent = useCallback(
    (frame: { id: string; event: string; data: string }) => {
      // An entry id is a resume point: the stream hook sends it as `Last-Event-ID`
      // and the server replays everything after it.
      if (frame.id) resumable.current = true;
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

  const onOpen = useCallback(() => {
    if (resumable.current) return;
    reconcileCriticalQueries();
  }, [reconcileCriticalQueries]);

  /* The stream hook holds its resume point for as long as one subscription
     lives, and starts a fresh one whenever the URL or `enabled` changes. Across
     that boundary the server is given no cursor and opens at the newest entry,
     so whatever moved in between is only recoverable by reconciling. */
  useEffect(() => {
    resumable.current = false;
  }, [streamUrl, visible]);

  // Nobody is watching a background tab, so nothing is streamed to it and
  // nothing is refetched for it. Coming back leaves a gap, which is what the
  // reconcile on the next open is for.
  const streamStatus = useEventStream(streamUrl, { enabled: visible, onEvent, onOpen });

  useEffect(() => {
    if (!visible) return;
    const timer = setInterval(reconcileCriticalQueries, RECOVERY_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [reconcileCriticalQueries, visible]);

  useEffect(() => {
    const ordinary = ordinaryTargets.current;
    const expensive = expensiveTargets.current;
    return () => {
      if (ordinaryTimer.current !== undefined) clearTimeout(ordinaryTimer.current);
      for (const target of expensive.values()) {
        if (target.timer !== undefined) clearTimeout(target.timer);
      }
      ordinary.clear();
      expensive.clear();
    };
  }, []);

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

/* Visibility comes from the query client's own focus manager rather than a second
   `visibilitychange` listener, so the stream and the cache agree on when the tab
   is in the background: it is already what pauses `refetchInterval`. */
function subscribeToVisibility(onChange: () => void): () => void {
  return focusManager.subscribe(onChange);
}

function isVisible(): boolean {
  return focusManager.isFocused();
}

function alwaysVisible(): boolean {
  return true;
}
