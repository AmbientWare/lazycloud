import { testQueryClient } from "@/test/query-client";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";

import { CollectionValueForm, editableJson, type MapEdit } from "./CollectionValueForm";

const entry = (value: unknown, revision: string): MapEdit["value"] => ({
  key: "config/limits",
  value: btoa(JSON.stringify(value)),
  revision,
  updated_at: "2026-09-30T00:00:00Z",
});

it("writes an edit only over the revision it was read at and reloads on conflict", async () => {
  const writes: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", async (request: Request) => {
    if (request.method === "PUT") {
      writes.push({ url: request.url, body: await request.json() });
      return Response.json({ code: "conflict", message: "The key changed" }, { status: 409 });
    }
    return Response.json(entry({ limit: 7 }, "12"));
  });
  const reloaded: MapEdit[] = [];
  render(
    <QueryClientProvider client={testQueryClient()}>
      <CollectionValueForm
        workspaceId="workspace"
        kind="maps"
        name="settings/prod"
        entry={{ key: "config/limits", value: entry({ limit: 5 }, "11") }}
        onDone={() => {}}
        onCancel={() => {}}
        onReload={(next) => reloaded.push(next)}
      />
    </QueryClientProvider>,
  );

  fireEvent.change(screen.getByLabelText("Value (JSON)"), { target: { value: '{"limit": 6}' } });
  fireEvent.click(screen.getByRole("button", { name: "Save value" }));

  expect(await screen.findByText("The key changed")).toBeVisible();
  expect(writes).toEqual([
    {
      url: expect.stringContaining(
        "/v1/workspaces/workspace/maps/settings%2Fprod/entries/config%2Flimits",
      ),
      body: { value: btoa('{"limit":6}'), if_absent: false, if_revision: "11" },
    },
  ]);
  fireEvent.click(screen.getByRole("button", { name: "Discard draft and reload" }));
  await waitFor(() => expect(reloaded.map((next) => next.value.revision)).toEqual(["12"]));
});

it("offers no JSON edit for a pickled Python value", () => {
  // cloudpickle output starts with the pickle protocol byte 0x80.
  const pickled = btoa(String.fromCharCode(0x80, 0x05, 0x95, 0x0b, 0x00, 0x00, 0x2e));
  expect(editableJson({ ...entry(null, "1"), value: pickled })).toBeNull();
  expect(editableJson(entry({ ready: true }, "1"))).toBe('{\n  "ready": true\n}');
});
