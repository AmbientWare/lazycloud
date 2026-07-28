import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import type {
  WorkspaceComputePolicy,
  WorkspaceComputePolicyUpdateRequest,
} from "@/lib/api/schemas";
import {
  computePolicyQueryOptions,
  computeQueryKeys,
  getComputePolicy,
  updateComputePolicy,
} from "@/lib/queries/compute";

import { regionsEqual } from "../region-selection";

export type ComputePolicyDraft = {
  defaultRegion: string;
  defaultInstanceType: string;
  initialCpuWorkers: number;
  minCpuWorkers: number;
  maxCpuInstances: number;
  maxGpuInstances: number;
  minFreeCpuMillicores: number;
  minFreeMemoryMib: number;
  allowedRegions: string[];
  allowedInstanceTypes: string;
  idleTimeoutSeconds: number;
  rootVolumeGib: number;
};

export type ComputePolicyDraftField = keyof ComputePolicyDraft;

export type ComputePolicyDraftUpdate =
  | { field: "defaultRegion"; value: string }
  | { field: "defaultInstanceType"; value: string }
  | { field: "initialCpuWorkers"; value: number }
  | { field: "minCpuWorkers"; value: number }
  | { field: "maxCpuInstances"; value: number }
  | { field: "maxGpuInstances"; value: number }
  | { field: "minFreeCpuMillicores"; value: number }
  | { field: "minFreeMemoryMib"; value: number }
  | { field: "allowedRegions"; value: string[] }
  | { field: "allowedInstanceTypes"; value: string }
  | { field: "idleTimeoutSeconds"; value: number }
  | { field: "rootVolumeGib"; value: number };

type EditorMode =
  "loading" | "ready" | "saving" | "saved" | "error" | "review" | "recovering" | "recovery_error";

type EditorState = {
  workspaceId: string;
  base: WorkspaceComputePolicy | null;
  draft: ComputePolicyDraft | null;
  dirtyFields: ComputePolicyDraftField[];
  mode: EditorMode;
  error: Error | null;
};

type SaveCommand = {
  workspaceId: string;
  request: WorkspaceComputePolicyUpdateRequest;
};

type SaveOutcome =
  | { kind: "saved"; workspaceId: string; policy: WorkspaceComputePolicy }
  | { kind: "conflict"; workspaceId: string; policy: WorkspaceComputePolicy }
  | { kind: "recovery_error"; workspaceId: string; error: Error };

export type ComputePolicyController = {
  policy: WorkspaceComputePolicy | null;
  draft: ComputePolicyDraft | null;
  dirtyFields: readonly ComputePolicyDraftField[];
  isLoading: boolean;
  isSaving: boolean;
  isSaved: boolean;
  isDirty: boolean;
  requiresReview: boolean;
  recoveryFailed: boolean;
  canSave: boolean;
  loadError: Error | null;
  saveError: Error | null;
  updateField: (update: ComputePolicyDraftUpdate) => void;
  save: () => void;
  review: () => void;
  retryLoad: () => void;
};

export function useComputePolicyController(workspaceId: string): ComputePolicyController {
  const queryClient = useQueryClient();
  const query = useQuery(computePolicyQueryOptions(workspaceId));
  const [state, setState] = useState<EditorState>(() => emptyState(workspaceId));
  const activeSaves = useRef(new Set<string>());
  const activeRecoveries = useRef(new Set<string>());
  const currentState = currentEditorState(state, workspaceId, query.data);
  if (state.workspaceId !== workspaceId) setState(currentState);

  const mutation = useMutation({
    mutationFn: async (command: SaveCommand): Promise<SaveOutcome> => {
      try {
        return {
          kind: "saved",
          workspaceId: command.workspaceId,
          policy: await updateComputePolicy(command.workspaceId, command.request),
        };
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409) throw error;
        try {
          const policy = await getComputePolicy(command.workspaceId);
          queryClient.setQueryData(computeQueryKeys.policy(command.workspaceId), policy);
          return { kind: "conflict", workspaceId: command.workspaceId, policy };
        } catch (recoveryError) {
          return {
            kind: "recovery_error",
            workspaceId: command.workspaceId,
            error: asError(recoveryError),
          };
        }
      }
    },
    onSuccess: (outcome) => {
      if (outcome.kind === "saved") {
        queryClient.setQueryData(computeQueryKeys.policy(outcome.workspaceId), outcome.policy);
      }
      setState((current) => {
        if (current.workspaceId !== outcome.workspaceId) return current;
        if (outcome.kind === "saved") {
          return {
            workspaceId: current.workspaceId,
            base: outcome.policy,
            draft: draftFromPolicy(outcome.policy),
            dirtyFields: [],
            mode: "saved",
            error: null,
          };
        }
        if (outcome.kind === "conflict") {
          return rebaseAuthority(current, outcome.policy, true);
        }
        return { ...current, mode: "recovery_error", error: outcome.error };
      });
    },
    onError: (error, command) => {
      setState((current) =>
        current.workspaceId === command.workspaceId
          ? { ...current, mode: "error", error: asError(error) }
          : current,
      );
    },
    onSettled: (_data, _error, command) => {
      activeSaves.current.delete(command.workspaceId);
    },
  });

  const updateField = (update: ComputePolicyDraftUpdate) => {
    setState((current) => {
      const owned = currentEditorState(current, workspaceId, query.data);
      if (!owned.base || !owned.draft) return owned;
      const draft = applyDraftUpdate(owned.draft, update);
      return {
        ...owned,
        draft,
        dirtyFields: changedFields(draft, owned.base),
        mode: "ready",
        error: null,
      };
    });
  };

  const save = () => {
    if (activeSaves.current.has(workspaceId)) return;
    const owned = currentState;
    if (!owned.base || !owned.draft || owned.dirtyFields.length === 0) return;
    if (owned.mode === "review" || owned.mode === "recovering" || owned.mode === "recovery_error") {
      return;
    }
    const request = updateRequest(owned.base, owned.draft);
    activeSaves.current.add(workspaceId);
    setState((current) => ({
      ...currentEditorState(current, workspaceId, query.data),
      mode: "saving",
      error: null,
    }));
    mutation.mutate({ workspaceId, request });
  };

  const review = () => {
    setState((current) => {
      const owned = currentEditorState(current, workspaceId, query.data);
      return owned.mode === "review" ? { ...owned, mode: "ready", error: null } : owned;
    });
  };

  const retryLoad = () => {
    if (activeRecoveries.current.has(workspaceId) || activeSaves.current.has(workspaceId)) {
      return;
    }
    activeRecoveries.current.add(workspaceId);
    setState((current) => ({
      ...currentEditorState(current, workspaceId, query.data),
      mode: "recovering",
      error: null,
    }));
    void getComputePolicy(workspaceId)
      .then((policy) => {
        queryClient.setQueryData(computeQueryKeys.policy(workspaceId), policy);
        setState((current) =>
          current.workspaceId === workspaceId
            ? rebaseAuthority(current, policy, current.dirtyFields.length > 0)
            : current,
        );
      })
      .catch((error: unknown) => {
        setState((current) =>
          current.workspaceId === workspaceId
            ? { ...current, mode: "recovery_error", error: asError(error) }
            : current,
        );
      })
      .finally(() => {
        activeRecoveries.current.delete(workspaceId);
      });
  };

  const hasDraft = currentState.draft !== null;
  const isDirty = currentState.dirtyFields.length > 0;
  return {
    policy: currentState.base,
    draft: currentState.draft,
    dirtyFields: currentState.dirtyFields,
    isLoading: !hasDraft && query.isPending,
    isSaving: currentState.mode === "saving",
    isSaved: currentState.mode === "saved",
    isDirty,
    requiresReview: currentState.mode === "review",
    recoveryFailed: currentState.mode === "recovery_error",
    canSave:
      isDirty &&
      currentState.mode !== "saving" &&
      currentState.mode !== "review" &&
      currentState.mode !== "recovering" &&
      currentState.mode !== "recovery_error",
    loadError: !hasDraft ? query.error : null,
    saveError:
      currentState.mode === "error" || currentState.mode === "recovery_error"
        ? currentState.error
        : null,
    updateField,
    save,
    review,
    retryLoad,
  };
}

function currentEditorState(
  state: EditorState,
  workspaceId: string,
  authority: WorkspaceComputePolicy | undefined,
): EditorState {
  if (state.workspaceId !== workspaceId) {
    return authority ? stateFromAuthority(workspaceId, authority) : emptyState(workspaceId);
  }
  return authority ? rebaseAuthority(state, authority) : state;
}

function emptyState(workspaceId: string): EditorState {
  return {
    workspaceId,
    base: null,
    draft: null,
    dirtyFields: [],
    mode: "loading",
    error: null,
  };
}

function stateFromAuthority(workspaceId: string, policy: WorkspaceComputePolicy): EditorState {
  return {
    workspaceId,
    base: policy,
    draft: draftFromPolicy(policy),
    dirtyFields: [],
    mode: "ready",
    error: null,
  };
}

function rebaseAuthority(
  state: EditorState,
  policy: WorkspaceComputePolicy,
  forceReview = false,
): EditorState {
  if (!state.base || !state.draft) return stateFromAuthority(state.workspaceId, policy);
  if (policiesEqual(state.base, policy)) return state;
  if (state.mode === "saving" && !forceReview) return state;
  if (state.dirtyFields.length === 0) return stateFromAuthority(state.workspaceId, policy);

  const authoritativeDraft = draftFromPolicy(policy);
  let draft = authoritativeDraft;
  for (const field of state.dirtyFields) {
    draft = copyDraftField(draft, state.draft, field);
  }
  const dirtyFields = changedFields(draft, policy);
  return {
    ...state,
    base: policy,
    draft,
    dirtyFields,
    mode: dirtyFields.length > 0 || forceReview ? "review" : "ready",
    error: null,
  };
}

function draftFromPolicy(policy: WorkspaceComputePolicy): ComputePolicyDraft {
  return {
    defaultRegion: policy.aws.default_region,
    defaultInstanceType: policy.aws.default_instance_type,
    initialCpuWorkers: policy.aws.initial_cpu_workers,
    minCpuWorkers: policy.aws.min_cpu_workers,
    maxCpuInstances: policy.aws.max_cpu_instances,
    maxGpuInstances: policy.aws.max_gpu_instances,
    minFreeCpuMillicores: policy.aws.min_free_cpu_millicores,
    minFreeMemoryMib: policy.aws.min_free_memory_mib,
    allowedRegions: [...policy.aws.allowed_regions],
    allowedInstanceTypes: policy.aws.allowed_instance_types.join(", "),
    idleTimeoutSeconds: policy.aws.idle_timeout_seconds,
    rootVolumeGib: policy.aws.root_volume_gib,
  };
}

function updateRequest(
  base: WorkspaceComputePolicy,
  draft: ComputePolicyDraft,
): WorkspaceComputePolicyUpdateRequest {
  return {
    expected_revision: base.revision,
    default_placement: base.default_placement,
    aws: {
      default_region: draft.defaultRegion,
      default_instance_type: draft.defaultInstanceType,
      initial_cpu_workers: draft.initialCpuWorkers,
      min_cpu_workers: draft.minCpuWorkers,
      max_cpu_instances: draft.maxCpuInstances,
      max_gpu_instances: draft.maxGpuInstances,
      min_free_cpu_millicores: draft.minFreeCpuMillicores,
      min_free_memory_mib: draft.minFreeMemoryMib,
      allowed_regions: [...draft.allowedRegions],
      allowed_instance_types: commaList(draft.allowedInstanceTypes),
      idle_timeout_seconds: draft.idleTimeoutSeconds,
      root_volume_gib: draft.rootVolumeGib,
    },
  };
}

const draftFields: readonly ComputePolicyDraftField[] = [
  "defaultRegion",
  "defaultInstanceType",
  "initialCpuWorkers",
  "minCpuWorkers",
  "maxCpuInstances",
  "maxGpuInstances",
  "minFreeCpuMillicores",
  "minFreeMemoryMib",
  "allowedRegions",
  "allowedInstanceTypes",
  "idleTimeoutSeconds",
  "rootVolumeGib",
];

function changedFields(
  draft: ComputePolicyDraft,
  base: WorkspaceComputePolicy,
): ComputePolicyDraftField[] {
  const baseDraft = draftFromPolicy(base);
  return draftFields.filter((field) => fieldChanged(field, draft, baseDraft));
}

function fieldChanged(
  field: ComputePolicyDraftField,
  draft: ComputePolicyDraft,
  base: ComputePolicyDraft,
): boolean {
  switch (field) {
    case "defaultRegion":
      return draft.defaultRegion !== base.defaultRegion;
    case "defaultInstanceType":
      return draft.defaultInstanceType !== base.defaultInstanceType;
    case "initialCpuWorkers":
      return draft.initialCpuWorkers !== base.initialCpuWorkers;
    case "minCpuWorkers":
      return draft.minCpuWorkers !== base.minCpuWorkers;
    case "maxCpuInstances":
      return draft.maxCpuInstances !== base.maxCpuInstances;
    case "maxGpuInstances":
      return draft.maxGpuInstances !== base.maxGpuInstances;
    case "minFreeCpuMillicores":
      return draft.minFreeCpuMillicores !== base.minFreeCpuMillicores;
    case "minFreeMemoryMib":
      return draft.minFreeMemoryMib !== base.minFreeMemoryMib;
    case "allowedRegions":
      return !regionsEqual(draft.allowedRegions, base.allowedRegions);
    case "allowedInstanceTypes":
      return !regionsEqual(
        commaList(draft.allowedInstanceTypes),
        commaList(base.allowedInstanceTypes),
      );
    case "idleTimeoutSeconds":
      return draft.idleTimeoutSeconds !== base.idleTimeoutSeconds;
    case "rootVolumeGib":
      return draft.rootVolumeGib !== base.rootVolumeGib;
  }
}

function copyDraftField(
  draft: ComputePolicyDraft,
  source: ComputePolicyDraft,
  field: ComputePolicyDraftField,
): ComputePolicyDraft {
  switch (field) {
    case "defaultRegion":
      return { ...draft, defaultRegion: source.defaultRegion };
    case "defaultInstanceType":
      return { ...draft, defaultInstanceType: source.defaultInstanceType };
    case "initialCpuWorkers":
      return { ...draft, initialCpuWorkers: source.initialCpuWorkers };
    case "minCpuWorkers":
      return { ...draft, minCpuWorkers: source.minCpuWorkers };
    case "maxCpuInstances":
      return { ...draft, maxCpuInstances: source.maxCpuInstances };
    case "maxGpuInstances":
      return { ...draft, maxGpuInstances: source.maxGpuInstances };
    case "minFreeCpuMillicores":
      return { ...draft, minFreeCpuMillicores: source.minFreeCpuMillicores };
    case "minFreeMemoryMib":
      return { ...draft, minFreeMemoryMib: source.minFreeMemoryMib };
    case "allowedRegions":
      return { ...draft, allowedRegions: [...source.allowedRegions] };
    case "allowedInstanceTypes":
      return { ...draft, allowedInstanceTypes: source.allowedInstanceTypes };
    case "idleTimeoutSeconds":
      return { ...draft, idleTimeoutSeconds: source.idleTimeoutSeconds };
    case "rootVolumeGib":
      return { ...draft, rootVolumeGib: source.rootVolumeGib };
  }
}

function applyDraftUpdate(
  draft: ComputePolicyDraft,
  update: ComputePolicyDraftUpdate,
): ComputePolicyDraft {
  switch (update.field) {
    case "defaultRegion":
      return { ...draft, defaultRegion: update.value };
    case "defaultInstanceType":
      return { ...draft, defaultInstanceType: update.value };
    case "initialCpuWorkers":
      return { ...draft, initialCpuWorkers: update.value };
    case "minCpuWorkers":
      return { ...draft, minCpuWorkers: update.value };
    case "maxCpuInstances":
      return { ...draft, maxCpuInstances: update.value };
    case "maxGpuInstances":
      return { ...draft, maxGpuInstances: update.value };
    case "minFreeCpuMillicores":
      return { ...draft, minFreeCpuMillicores: update.value };
    case "minFreeMemoryMib":
      return { ...draft, minFreeMemoryMib: update.value };
    case "allowedRegions":
      return { ...draft, allowedRegions: [...update.value] };
    case "allowedInstanceTypes":
      return { ...draft, allowedInstanceTypes: update.value };
    case "idleTimeoutSeconds":
      return { ...draft, idleTimeoutSeconds: update.value };
    case "rootVolumeGib":
      return { ...draft, rootVolumeGib: update.value };
  }
}

function policiesEqual(left: WorkspaceComputePolicy, right: WorkspaceComputePolicy): boolean {
  return (
    left.revision === right.revision &&
    left.default_placement === right.default_placement &&
    left.created_at === right.created_at &&
    left.updated_at === right.updated_at &&
    left.aws.default_region === right.aws.default_region &&
    left.aws.default_instance_type === right.aws.default_instance_type &&
    left.aws.initial_cpu_workers === right.aws.initial_cpu_workers &&
    left.aws.min_cpu_workers === right.aws.min_cpu_workers &&
    left.aws.max_cpu_instances === right.aws.max_cpu_instances &&
    left.aws.max_gpu_instances === right.aws.max_gpu_instances &&
    left.aws.min_free_cpu_millicores === right.aws.min_free_cpu_millicores &&
    left.aws.min_free_memory_mib === right.aws.min_free_memory_mib &&
    left.aws.idle_timeout_seconds === right.aws.idle_timeout_seconds &&
    left.aws.root_volume_gib === right.aws.root_volume_gib &&
    regionsEqual(left.aws.allowed_regions, right.aws.allowed_regions) &&
    regionsEqual(left.aws.allowed_instance_types, right.aws.allowed_instance_types)
  );
}

function commaList(value: string): string[] {
  return [
    ...new Set(
      value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Compute policy request failed");
}
