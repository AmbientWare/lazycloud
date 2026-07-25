import { Badge } from "@/components/ui/badge";
import type { RowValue } from "@/lib/api/resources";
import { displayValue, statusTone } from "@/lib/format";

/** The only filled chip in the app; strictly for status values. */
export function StatusChip({ status, live = false }: { status: RowValue; live?: boolean }) {
  return (
    <Badge tone={statusTone(status)} className="gap-1.5">
      {live ? <span className="pulse-live size-1.5 rounded-full bg-current" aria-hidden="true" /> : null}
      {displayValue(status)}
    </Badge>
  );
}
