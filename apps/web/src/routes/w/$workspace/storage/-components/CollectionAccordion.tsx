import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ExpandableRow } from "@/components/shared/ExpandableRow";

import { ApiErrorNotice } from "@/components/shared/ApiErrorNotice";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { Skeleton } from "@/components/ui/skeleton";
import type { ResourceConfig, ResourceRow } from "@/lib/api/resources";
import { displayValue } from "@/lib/format";
import { resourceQueryOptions } from "@/lib/queries/resources";

import { MapInspector, QueueInspector } from "./CollectionInspectors";

export function CollectionAccordion({
  config,
  workspaceId,
}: {
  config: ResourceConfig;
  workspaceId: string;
}) {
  const query = useQuery(resourceQueryOptions(config, workspaceId));
  const rows = query.data ?? [];
  const [openId, setOpenId] = useState<string | null>(null);
  const singular = config.key === "queues" ? "queue" : "map";

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:min-h-0">
      <section
        aria-label={`${config.title} collection`}
        data-collection-scroll=""
        className="min-h-[24rem] flex-1 overflow-visible lg:min-h-0 lg:overflow-y-auto"
      >
        <div className="flex min-h-11 items-center gap-2 border-b border-border px-3 py-2">
          <h2 className="text-sm font-medium">{config.title}</h2>
          <span className="mono text-xs text-muted-foreground">{rows.length}</span>
        </div>
        {query.isError && query.data ? (
          <ApiErrorNotice
            compact
            error={query.error}
            title="Collections could not be refreshed"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : null}
        {query.isPending ? (
          <CollectionSkeleton />
        ) : query.isError && !query.data ? (
          <ApiErrorNotice
            error={query.error}
            title="Collections could not be loaded"
            onRetry={() => void query.refetch()}
            retrying={query.isFetching}
          />
        ) : rows.length === 0 ? (
          <PanelEmpty message={`No ${config.title.toLowerCase()}`} className="p-8" />
        ) : (
          <div className="divide-y divide-border/70">
            {rows.map((row) => {
              const open = openId === row.id;
              const name = String(row.name ?? row.id);
              return (
                <ExpandableRow
                  key={row.id}
                  open={open}
                  onOpenChange={(next) => setOpenId(next ? row.id : null)}
                  label={`${name} ${singular} inspector`}
                  summary={<CollectionSummary config={config} row={row} />}
                >
                  {config.key === "queues" ? (
                    <QueueInspector
                      workspaceId={workspaceId}
                      name={name}
                      oldestMessageAgeSeconds={numberValue(row.oldest_message_age_seconds)}
                      putRatePerMinute={numberValue(row.put_rate_per_minute) ?? 0}
                    />
                  ) : (
                    <MapInspector
                      workspaceId={workspaceId}
                      name={name}
                      sizeBytes={numberValue(row.size_bytes) ?? 0}
                      keyCount={numberValue(row.keys) ?? 0}
                      expiringKeys={numberValue(row.expiring_keys) ?? 0}
                      nearestExpirySeconds={numberValue(row.nearest_expiry_seconds)}
                    />
                  )}
                </ExpandableRow>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function CollectionSummary({ config, row }: { config: ResourceConfig; row: ResourceRow }) {
  const name = String(row.name ?? row.id);
  const amount = config.key === "queues" ? row.size : row.keys;
  const unit = collectionUnit(config.key, amount);

  return (
    <>
      <span className="mono min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">
        {name}
      </span>
      <span className="shrink-0 text-right">
        <span className="readout block text-sm text-foreground">{displayValue(amount)}</span>
        <span className="block text-[10px] text-muted-foreground">{unit}</span>
      </span>
    </>
  );
}

function collectionUnit(configKey: string, amount: ResourceRow[string]): string {
  const singular = configKey === "queues" ? "message" : "key";
  return Number(amount) === 1 ? singular : `${singular}s`;
}

function numberValue(value: ResourceRow[string]): number | null {
  return typeof value === "number" ? value : null;
}

function CollectionSkeleton() {
  return (
    <div aria-hidden="true" className="divide-y divide-border/60">
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className="flex items-center gap-3 px-3 py-3">
          <Skeleton className="size-3.5" />
          <Skeleton className="h-4 flex-1" />
          <Skeleton className="h-7 w-10" />
        </div>
      ))}
    </div>
  );
}
