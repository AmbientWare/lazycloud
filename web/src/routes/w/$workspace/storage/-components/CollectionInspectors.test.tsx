import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { MapInspector } from "./CollectionInspectors";

it("loads the value of an empty-string map key", async () => {
  const queryClient = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  queryClient.setQueryData(workspaceQueryKeys.collections.mapCount("workspace-1", "map"), {
    count: 1,
  });
  queryClient.setQueryData([...workspaceQueryKeys.collections.mapKeys("workspace-1", "map"), ""], {
    pages: [{ data: [""], next: null }],
    pageParams: [""],
  });
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    Response.json({
      value_base64: btoa("value for the empty key"),
      revision: "first",
      expires_at: null,
    }),
  );
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
  expect(screen.getByRole("button", { name: "Edit value" })).toBeDisabled();
  expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/maps/map/entry?key=&workspace=workspace-1");
});

it("keeps the user's draft and original revision when live data changes during an edit", async () => {
  const queryClient = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  queryClient.setQueryData(workspaceQueryKeys.collections.mapCount("workspace-1", "map"), {
    count: 1,
  });
  queryClient.setQueryData([...workspaceQueryKeys.collections.mapKeys("workspace-1", "map"), ""], {
    pages: [{ data: ["key"], next: null }],
    pageParams: [""],
  });
  const valueKey = workspaceQueryKeys.collections.mapValue("workspace-1", "map", "key");
  queryClient.setQueryData(valueKey, {
    value_base64: btoa('{"value":1}'),
    revision: "opened",
    expires_at: null,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>().mockImplementation(async (_input, init) => {
      const request = JSON.parse(String(init?.body));
      if (request.if_revision !== "worker") {
        return Response.json({ detail: "Map key changed. Reload before saving." }, { status: 409 });
      }
      return Response.json({});
    }),
  );
  render(
    <QueryClientProvider client={queryClient}>
      <MapInspector
        workspaceId="workspace-1"
        name="map"
        sizeBytes={11}
        expiringKeys={0}
        nearestExpirySeconds={null}
      />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit value" }));
  fireEvent.change(screen.getByLabelText("Value (JSON)"), { target: { value: '{"value":2}' } });
  act(() =>
    queryClient.setQueryData(valueKey, {
      value_base64: btoa('{"value":3}'),
      revision: "worker",
      expires_at: null,
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Save value" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("alert")).toHaveTextContent("Map key changed");
  expect(screen.getByLabelText("Value (JSON)")).toHaveValue('{"value":2}');
  expect(screen.getByRole("button", { name: "Discard draft and reload" })).toBeEnabled();
});
