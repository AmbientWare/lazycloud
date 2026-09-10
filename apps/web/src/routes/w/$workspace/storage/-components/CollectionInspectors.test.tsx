import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { MapInspector } from "./CollectionInspectors";

it("loads the value of an empty-string map key", async () => {
  const queryClient = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  queryClient.setQueryData(workspaceQueryKeys.collections.mapCount("workspace-1", "map"), {
    count: 1,
  });
  queryClient.setQueryData(workspaceQueryKeys.collections.mapKeys("workspace-1", "map"), {
    keys: [""],
  });
  const fetchMock = vi
    .fn<typeof fetch>()
    .mockResolvedValue(Response.json({ value_base64: btoa("value for the empty key") }));
  vi.stubGlobal("fetch", fetchMock);
  render(
    <QueryClientProvider client={queryClient}>
      <MapInspector
        workspaceId="workspace-1"
        name="map"
        sizeBytes={23}
        expiringKeys={0}
        nearestExpirySeconds={null}
      />
    </QueryClientProvider>,
  );

  await screen.findByText("value for the empty key");
  expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/maps/map/get?key=&workspace=workspace-1");
});
