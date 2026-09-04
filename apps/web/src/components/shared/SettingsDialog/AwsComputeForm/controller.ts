import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "@/lib/api/client";
import type {
  AwsComputeConfiguration,
  AwsComputeConfigurationUpdateRequest,
} from "@/lib/api/schemas";
import {
  accountComputeQueryKeys,
  awsConnectionQueryOptions,
  getAwsConnection,
  updateAwsComputeConfiguration,
} from "@/lib/queries/compute";

import { regionsEqual } from "../region-selection";

const DISCONNECTED_MESSAGE = "The AWS account is no longer connected";

export type AwsComputeDraft = {
  defaultRegion: string;
  defaultInstanceType: string;
  initialCpuWorkers: number;
  minCpuWorkers: number;
  maxCpuInstances: number | null;
  maxGpuInstances: number | null;
  minFreeCpuMillicores: number;
  minFreeMemoryMib: number;
  allowedRegions: string[];
  allowedInstanceTypes: string;
  idleTimeoutSeconds: number;
  rootVolumeGib: number;
};

export type AwsComputeDraftField = keyof AwsComputeDraft;

export type AwsComputeDraftUpdate =
  | { field: "defaultRegion"; value: string }
  | { field: "defaultInstanceType"; value: string }
  | { field: "initialCpuWorkers"; value: number }
  | { field: "minCpuWorkers"; value: number }
  | { field: "maxCpuInstances"; value: number | null }
  | { field: "maxGpuInstances"; value: number | null }
  | { field: "minFreeCpuMillicores"; value: number }
  | { field: "minFreeMemoryMib"; value: number }
  | { field: "allowedRegions"; value: string[] }
  | { field: "allowedInstanceTypes"; value: string }
  | { field: "idleTimeoutSeconds"; value: number }
  | { field: "rootVolumeGib"; value: number };

type EditorMode =
  "loading" | "ready" | "saving" | "saved" | "error" | "review" | "recovering" | "recovery_error";

type EditorState = {
  base: AwsComputeConfiguration | null;
  draft: AwsComputeDraft | null;
  dirtyFields: AwsComputeDraftField[];
  mode: EditorMode;
  error: Error | null;
};

type SaveOutcome =
  | { kind: "saved"; configuration: AwsComputeConfiguration }
  | { kind: "conflict"; configuration: AwsComputeConfiguration }
  | { kind: "recovery_error"; error: Error };

export type AwsComputeController = {
  configuration: AwsComputeConfiguration | null;
  draft: AwsComputeDraft | null;
  dirtyFields: readonly AwsComputeDraftField[];
  isLoading: boolean;
  isSaving: boolean;
  isSaved: boolean;
  isDirty: boolean;
  requiresReview: boolean;
  recoveryFailed: boolean;
  canSave: boolean;
  loadError: Error | null;
  saveError: Error | null;
  updateField: (update: AwsComputeDraftUpdate) => void;
  save: () => void;
  review: () => void;
  retryLoad: () => void;
};

/**
 * Editor for the connected account's provisioning limits and defaults.
 *
 * The configuration is the account's, so there is no workspace dimension here.
 * Its authority is the connection record the settings surface already polls;
 * a field the person edited survives an authority change and is held for
 * explicit review rather than being silently overwritten or silently kept.
 */
export function useAwsComputeController(): AwsComputeController {
  const queryClient = useQueryClient();
  const query = useQuery(awsConnectionQueryOptions());
  const [state, setState] = useState<EditorState>(emptyState);
  const saveInFlight = useRef(false);
  const recoveryInFlight = useRef(false);
  const authority = query.data?.compute ?? null;
  const currentState = authority ? rebaseAuthority(state, authority) : state;

  const mutation = useMutation({
    mutationFn: async (request: AwsComputeConfigurationUpdateRequest): Promise<SaveOutcome> => {
      try {
        const connection = await updateAwsComputeConfiguration(request);
        queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), connection);
        return { kind: "saved", configuration: connection.compute };
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409) throw error;
        try {
          const connection = await getAwsConnection();
          if (connection === null) {
            return { kind: "recovery_error", error: new Error(DISCONNECTED_MESSAGE) };
          }
          queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), connection);
          return { kind: "conflict", configuration: connection.compute };
        } catch (recoveryError) {
          return { kind: "recovery_error", error: asError(recoveryError) };
        }
      }
    },
    onSuccess: (outcome) => {
      setState((current) => {
        if (outcome.kind === "saved") {
          return {
            base: outcome.configuration,
            draft: draftFrom(outcome.configuration),
            dirtyFields: [],
            mode: "saved",
            error: null,
          };
        }
        if (outcome.kind === "conflict") {
          return rebaseAuthority(current, outcome.configuration, true);
        }
        return { ...current, mode: "recovery_error", error: outcome.error };
      });
    },
    onError: (error) => {
      setState((current) => ({ ...current, mode: "error", error: asError(error) }));
    },
    onSettled: () => {
      saveInFlight.current = false;
    },
  });

  const updateField = (update: AwsComputeDraftUpdate) => {
    setState((current) => {
      const owned = authority ? rebaseAuthority(current, authority) : current;
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
    if (saveInFlight.current) return;
    const owned = currentState;
    if (!owned.base || !owned.draft || owned.dirtyFields.length === 0) return;
    if (owned.mode === "review" || owned.mode === "recovering" || owned.mode === "recovery_error") {
      return;
    }
    const request = updateRequest(owned.base, owned.draft);
    saveInFlight.current = true;
    setState((current) => ({
      ...(authority ? rebaseAuthority(current, authority) : current),
      mode: "saving",
      error: null,
    }));
    mutation.mutate(request);
  };

  const review = () => {
    setState((current) => {
      const owned = authority ? rebaseAuthority(current, authority) : current;
      return owned.mode === "review" ? { ...owned, mode: "ready", error: null } : owned;
    });
  };

  const retryLoad = () => {
    if (recoveryInFlight.current || saveInFlight.current) return;
    recoveryInFlight.current = true;
    setState((current) => ({
      ...(authority ? rebaseAuthority(current, authority) : current),
      mode: "recovering",
      error: null,
    }));
    void getAwsConnection()
      .then((connection) => {
        if (connection === null) {
          setState((current) => ({
            ...current,
            mode: "recovery_error",
            error: new Error(DISCONNECTED_MESSAGE),
          }));
          return;
        }
        queryClient.setQueryData(accountComputeQueryKeys.awsConnection(), connection);
        setState((current) =>
          rebaseAuthority(current, connection.compute, current.dirtyFields.length > 0),
        );
      })
      .catch((error: unknown) => {
        setState((current) => ({
          ...current,
          mode: "recovery_error",
          error: asError(error),
        }));
      })
      .finally(() => {
        recoveryInFlight.current = false;
      });
  };

  const hasDraft = currentState.draft !== null;
  const isDirty = currentState.dirtyFields.length > 0;
  return {
    configuration: currentState.base,
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

function emptyState(): EditorState {
  return { base: null, draft: null, dirtyFields: [], mode: "loading", error: null };
}

function stateFromAuthority(configuration: AwsComputeConfiguration): EditorState {
  return {
    base: configuration,
    draft: draftFrom(configuration),
    dirtyFields: [],
    mode: "ready",
    error: null,
  };
}

function rebaseAuthority(
  state: EditorState,
  configuration: AwsComputeConfiguration,
  forceReview = false,
): EditorState {
  if (!state.base || !state.draft) return stateFromAuthority(configuration);
  if (configurationsEqual(state.base, configuration)) return state;
  if (state.mode === "saving" && !forceReview) return state;
  if (state.dirtyFields.length === 0) return stateFromAuthority(configuration);

  let draft = draftFrom(configuration);
  for (const field of state.dirtyFields) {
    draft = copyDraftField(draft, state.draft, field);
  }
  const dirtyFields = changedFields(draft, configuration);
  return {
    ...state,
    base: configuration,
    draft,
    dirtyFields,
    mode: dirtyFields.length > 0 || forceReview ? "review" : "ready",
    error: null,
  };
}

function draftFrom(configuration: AwsComputeConfiguration): AwsComputeDraft {
  return {
    defaultRegion: configuration.default_region,
    defaultInstanceType: configuration.default_instance_type,
    initialCpuWorkers: configuration.initial_cpu_workers,
    minCpuWorkers: configuration.min_cpu_workers,
    maxCpuInstances: configuration.max_cpu_instances,
    maxGpuInstances: configuration.max_gpu_instances,
    minFreeCpuMillicores: configuration.min_free_cpu_millicores,
    minFreeMemoryMib: configuration.min_free_memory_mib,
    allowedRegions: [...configuration.allowed_regions],
    allowedInstanceTypes: configuration.allowed_instance_types.join(", "),
    idleTimeoutSeconds: configuration.idle_timeout_seconds,
    rootVolumeGib: configuration.root_volume_gib,
  };
}

function updateRequest(
  base: AwsComputeConfiguration,
  draft: AwsComputeDraft,
): AwsComputeConfigurationUpdateRequest {
  return {
    expected_revision: base.revision,
    compute: {
      revision: base.revision,
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

const draftFields: readonly AwsComputeDraftField[] = [
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
  draft: AwsComputeDraft,
  base: AwsComputeConfiguration,
): AwsComputeDraftField[] {
  const baseDraft = draftFrom(base);
  return draftFields.filter((field) => fieldChanged(field, draft, baseDraft));
}

function fieldChanged(
  field: AwsComputeDraftField,
  draft: AwsComputeDraft,
  base: AwsComputeDraft,
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
  draft: AwsComputeDraft,
  source: AwsComputeDraft,
  field: AwsComputeDraftField,
): AwsComputeDraft {
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

function applyDraftUpdate(draft: AwsComputeDraft, update: AwsComputeDraftUpdate): AwsComputeDraft {
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

function configurationsEqual(
  left: AwsComputeConfiguration,
  right: AwsComputeConfiguration,
): boolean {
  return (
    left.revision === right.revision &&
    left.default_region === right.default_region &&
    left.default_instance_type === right.default_instance_type &&
    left.initial_cpu_workers === right.initial_cpu_workers &&
    left.min_cpu_workers === right.min_cpu_workers &&
    left.max_cpu_instances === right.max_cpu_instances &&
    left.max_gpu_instances === right.max_gpu_instances &&
    left.min_free_cpu_millicores === right.min_free_cpu_millicores &&
    left.min_free_memory_mib === right.min_free_memory_mib &&
    left.idle_timeout_seconds === right.idle_timeout_seconds &&
    left.root_volume_gib === right.root_volume_gib &&
    regionsEqual(left.allowed_regions, right.allowed_regions) &&
    regionsEqual(left.allowed_instance_types, right.allowed_instance_types)
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
  return error instanceof Error ? error : new Error("AWS compute configuration request failed");
}
