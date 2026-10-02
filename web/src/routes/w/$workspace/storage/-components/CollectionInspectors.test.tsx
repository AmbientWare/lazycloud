import { testQueryClient } from "@/test/query-client";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { workspaceQueryKeys } from "@/lib/queries/workspace-keys";

import { MapInspector } from "./CollectionInspectors";

it("keeps the user's draft and original revision when live data changes during an edit", async () => {
  const queryClient = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  queryClient.setQueryData(workspaceQueryKeys.collections.map("workspace", "map"), {
    name: "map",
    count: 1,
    size_bytes: 11,
    expiring_count: 0,
  });
  queryClient.setQueryData([...workspaceQueryKeys.collections.mapKeys("workspace", "map"), ""], {
    pages: [{ keys: ["key"] }],
    pageParams: [""],
  });
  const valueKey = workspaceQueryKeys.collections.mapValue("workspace", "map", "key");
  const entry = { key: "key", updated_at: "2026-09-07T00:00:00Z" };
  queryClient.setQueryData(valueKey, { ...entry, value: btoa('{"value":1}'), revision: "1" });
  vi.stubGlobal(
    "fetch",
    vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const request = await (input as Request).json();
      if (request.if_revision !== "2") {
        return Response.json(
          { code: "conflict", message: "Map key changed. Reload before saving." },
          { status: 409 },
        );
      }
      return Response.json({ revision: "3" });
    }),
  );
  render(
    <QueryClientProvider client={queryClient}>
      <MapInspector workspace="workspace" name="map" />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Edit value" }));
  fireEvent.change(screen.getByLabelText("Value (JSON)"), { target: { value: '{"value":2}' } });
  act(() =>
    queryClient.setQueryData(valueKey, { ...entry, value: btoa('{"value":3}'), revision: "2" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Save value" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("alert")).toHaveTextContent("Map key changed");
  expect(screen.getByLabelText("Value (JSON)")).toHaveValue('{"value":2}');
  expect(screen.getByRole("button", { name: "Discard draft and reload" })).toBeEnabled();
});

it("adds a map key only once it has a name", () => {
  const queryClient = testQueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  queryClient.setQueryData(workspaceQueryKeys.collections.map("workspace", "map"), {
    name: "map",
    count: 0,
    size_bytes: 0,
    expiring_count: 0,
  });
  queryClient.setQueryData([...workspaceQueryKeys.collections.mapKeys("workspace", "map"), ""], {
    pages: [{ keys: [] }],
    pageParams: [""],
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MapInspector workspace="workspace" name="map" />
    </QueryClientProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Add key" }));
  fireEvent.change(screen.getByLabelText("Value (JSON)"), { target: { value: "{}" } });
  const submit = screen.getAllByRole("button", { name: "Add key" }).at(-1);
  expect(submit).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Key"), { target: { value: "k" } });
  expect(submit).toBeEnabled();
});
