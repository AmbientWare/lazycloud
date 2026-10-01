import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { rememberWorkspaces } from "@/lib/api/workspaces";
import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { MapInspector } from "./CollectionInspectors";

beforeEach(() => rememberWorkspaces([{ id: "workspace-1", name: "workspace" }]));

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
    vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const request = await (input as Request).json();
      if (request.if_revision !== "worker") {
        return Response.json(
          { code: "conflict", message: "Map key changed. Reload before saving." },
          { status: 409 },
        );
      }
      return Response.json({ revision: "next" });
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
