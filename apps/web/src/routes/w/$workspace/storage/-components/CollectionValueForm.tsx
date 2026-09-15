import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";
import type { MapEntry } from "@/lib/api/schemas";
import { base64ToBytes } from "@/lib/files";
import {
  mapValueQueryOptions,
  parseCollectionJson,
  putQueueMessage,
  refreshCollection,
  setMapValue,
} from "@/lib/queries/collections";

export type MapEdit = { key: string; value: MapEntry };

export function editableJson(value: MapEntry): string | null {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(
      base64ToBytes(value.value_base64),
    );
    return JSON.stringify(JSON.parse(parseCollectionJson(text)), null, 2);
  } catch {
    return null;
  }
}

export function CollectionValueForm({
  workspaceId,
  kind,
  name: fixedName,
  entry,
  onDone,
  onCancel,
  onReload,
}: {
  workspaceId: string;
  kind: "maps" | "queues";
  name?: string;
  entry?: MapEdit;
  onDone: (name: string, key: string) => void;
  onCancel: () => void;
  onReload?: (entry: MapEdit) => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(fixedName ?? "");
  const [key, setKey] = useState(entry?.key ?? "");
  const [json, setJson] = useState(entry ? (editableJson(entry.value) ?? "") : "");
  const [expiry, setExpiry] = useState(entry ? "keep" : "custom");
  const [seconds, setSeconds] = useState("604800");
  const [validation, setValidation] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      if (kind === "queues") await putQueueMessage(workspaceId, name.trim(), json);
      else
        await setMapValue(
          workspaceId,
          name.trim(),
          key,
          json,
          expiry === "keep" ? null : expiry === "never" ? 0 : Number(seconds),
          entry?.value.revision,
        );
    },
    onSuccess: async () => {
      await refreshCollection(client, workspaceId, kind, name.trim());
      onDone(name.trim(), key);
    },
  });
  const reload = useMutation({
    mutationFn: async () => {
      const value = await client.fetchQuery({
        ...mapValueQueryOptions(workspaceId, name, key),
        staleTime: 0,
      });
      onReload?.({ key, value });
    },
  });
  const busy = save.isPending || reload.isPending;
  const conflict = save.error instanceof ApiError && save.error.status === 409;

  return (
    <form
      className="grid gap-3 rounded-md border border-border bg-muted/20 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        try {
          parseCollectionJson(json);
          setValidation(null);
          save.mutate();
        } catch (error) {
          setValidation(error instanceof Error ? error.message : "Enter valid JSON.");
        }
      }}
    >
      <fieldset disabled={busy} className="grid min-w-0 gap-3">
        {!fixedName ? (
          <label className="grid gap-1.5 text-xs">
            {kind === "maps" ? "Map name" : "Queue name"}
            <Input
              autoFocus
              required
              value={name}
              onChange={(event) => setName(event.target.value)}
              className="mono"
            />
          </label>
        ) : null}
        {kind === "maps" ? (
          <label className="grid gap-1.5 text-xs">
            Key
            <Input
              value={key}
              disabled={entry !== undefined}
              onChange={(event) => setKey(event.target.value)}
              className="mono"
            />
          </label>
        ) : null}
        <label className="grid min-w-0 gap-1.5 text-xs">
          {kind === "maps" ? "Value (JSON)" : "Message (JSON)"}
          <textarea
            required
            rows={7}
            value={json}
            onChange={(event) => setJson(event.target.value)}
            spellCheck={false}
            placeholder={'{"status": "ready"}'}
            className="mono w-full rounded-md border border-input bg-background p-3 text-xs outline-none focus:border-ring"
          />
        </label>
        {kind === "maps" ? (
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1.5 text-xs">
              Expiry
              <select
                value={expiry}
                onChange={(event) => setExpiry(event.target.value)}
                className="h-9 rounded-md border border-input bg-background px-2"
              >
                {entry ? <option value="keep">Keep existing expiry</option> : null}
                <option value="custom">Expire after</option>
                <option value="never">No expiry</option>
              </select>
            </label>
            {expiry === "custom" ? (
              <label className="grid gap-1.5 text-xs">
                Seconds, up to 7 days
                <Input
                  type="number"
                  min={1}
                  max={604800}
                  step={1}
                  required
                  value={seconds}
                  onChange={(event) => setSeconds(event.target.value)}
                  className="w-40"
                />
              </label>
            ) : null}
          </div>
        ) : null}
        {entry ? (
          <p className="text-xs text-muted-foreground">
            Current expiry:{" "}
            {entry.value.expires_at
              ? new Date(entry.value.expires_at).toLocaleString()
              : "No expiry"}
          </p>
        ) : null}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="submit" size="sm" disabled={!name.trim() || !json.trim()}>
            {save.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
            {entry ? "Save value" : kind === "maps" ? "Add key" : "Add message"}
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </fieldset>
      {validation || save.isError ? (
        <p role="alert" className="text-xs text-destructive">
          {validation ?? save.error?.message}
        </p>
      ) : null}
      {conflict && entry ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={busy}
          onClick={() => reload.mutate()}
        >
          Discard draft and reload
        </Button>
      ) : null}
      {reload.isError ? (
        <p role="alert" className="text-xs text-destructive">
          {reload.error.message}
        </p>
      ) : null}
    </form>
  );
}
