import { Badge } from "@/components/ui/badge";
import type { RowValue } from "@/lib/api/resources";
import { displayValue, statusTone } from "@/lib/format";

/** The only filled chip in the app; strictly for status values. */
export function StatusChip({ status }: { status: RowValue }) {
  return (
    <Badge tone={statusTone(status)} className="gap-1.5">
      {displayValue(status)}
    </Badge>
  );
}
