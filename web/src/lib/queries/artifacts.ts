import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { nextPageCursor, selectPages } from "@/lib/queries/infinite-list";

import { workspaceQueryKeys } from "./workspace-keys";

export type Artifact = Schemas["Artifact"];

export type ArtifactFilters = {
  search?: string;
  task_id?: string;
  app?: string;
  content_type?: string;
  created_after?: string;
  created_before?: string;
};

export function artifactsQuery(workspace: string, filters: ArtifactFilters) {
  return infiniteQueryOptions({
    queryKey: workspaceQueryKeys.storage.artifactList(workspace, filters),
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/workspaces/{workspace}/artifacts", {
          params: {
            path: { workspace },
            query: {
              search: filters.search || undefined,
              task_id: filters.task_id || undefined,
              app: filters.app || undefined,
              content_type: filters.content_type || undefined,
              created_after: filters.created_after || undefined,
              created_before: filters.created_before || undefined,
              limit: 100,
              cursor: pageParam || undefined,
            },
          },
        }),
      ),
    getNextPageParam: nextPageCursor,
    refetchInterval: 15_000,
  });
}

export function selectArtifacts(
  data: { pages: readonly Schemas["ArtifactPage"][] } | undefined,
  hasNextPage: boolean | undefined,
) {
  return selectPages(
    data,
    (page) => page.artifacts,
    hasNextPage,
    (artifact) => artifact.id,
  );
}

export function artifactSummaryQuery(workspace: string) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.artifactSummary(workspace),
    queryFn: () =>
      ok(
        api.GET("/v1/workspaces/{workspace}/artifacts/summary", {
          params: { path: { workspace } },
        }),
      ),
    refetchInterval: 15_000,
  });
}

/** Deletes up to 100 artifacts in one request; ids already gone are left out of the answer. */
export function deleteArtifacts(workspace: string, ids: string[]): Promise<string[]> {
  return ok(
    api.POST("/v1/workspaces/{workspace}/artifacts/delete", {
      params: { path: { workspace } },
      body: { ids },
    }),
  ).then((result) => result.deleted);
}

const URL_LIFETIME_SECONDS = 300;

/**
 * A presigned GET for the artifact's bytes. `download` asks the store to send
 * it as an attachment. The URL names the object store and carries no session.
 */
export async function artifactUrl(
  workspace: string,
  artifact: string,
  download: boolean,
): Promise<string> {
  const presigned = await ok(
    api.POST("/v1/workspaces/{workspace}/artifacts/{artifact}/url", {
      params: { path: { workspace, artifact } },
      body: { download, expires_seconds: URL_LIFETIME_SECONDS },
    }),
  );
  return presigned.url;
}

export type PreviewKind = "image" | "pdf" | "text";

/**
 * What a preview shows: images and PDFs load from the presigned URL itself;
 * text is read through it, which needs the store to allow this origin. Reused
 * until shortly before the URL expires.
 */
export function artifactPreviewQuery(workspace: string, artifact: string, kind: PreviewKind) {
  return queryOptions({
    queryKey: workspaceQueryKeys.storage.artifactUrl(workspace, artifact),
    queryFn: async ({ signal }) => {
      const url = await artifactUrl(workspace, artifact, false);
      if (kind === "image") {
        const image = new Image();
        image.src = url;
        await image.decode();
      }
      if (kind !== "text") return { url, text: null };
      const response = await fetch(url, { signal });
      if (!response.ok) throw new Error(`Could not read the file (${response.status})`);
      return { url, text: await response.text() };
    },
    staleTime: (URL_LIFETIME_SECONDS - 60) * 1_000,
    gcTime: 60_000,
    retry: false,
  });
}

/** Save the artifact through the browser's own download, without reading it into the page. */
export async function downloadArtifact(workspace: string, artifact: string): Promise<void> {
  const link = document.createElement("a");
  link.href = await artifactUrl(workspace, artifact, true);
  link.rel = "noopener noreferrer";
  document.body.append(link);
  link.click();
  link.remove();
}
