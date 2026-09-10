import { useState } from "react";

import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatCostNanos } from "@/lib/money";

export function AmountSelect({
  id,
  label,
  value,
  onChange,
  presets,
  minimum = 0,
  maximum = Infinity,
  allowNoLimit = false,
  disabled,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  presets: readonly number[];
  minimum?: number;
  maximum?: number;
  allowNoLimit?: boolean;
  disabled?: boolean;
}) {
  const amounts = presets.filter((amount) => amount >= minimum && amount <= maximum);
  const [custom, setCustom] = useState(false);
  const selection =
    custom || (value !== "" && !amounts.includes(Number(value)))
      ? "custom"
      : value === ""
        ? allowNoLimit
          ? "unlimited"
          : "custom"
        : String(Number(value));

  return (
    <div className="min-w-0 flex-1 space-y-2">
      <Select
        value={selection}
        disabled={disabled}
        onValueChange={(next) => {
          setCustom(next === "custom");
          if (next !== "custom") onChange(next === "unlimited" ? "" : next);
        }}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="start">
          {allowNoLimit ? <SelectItem value="unlimited">No limit</SelectItem> : null}
          {amounts.map((amount) => (
            <SelectItem key={amount} value={String(amount)}>
              {formatCostNanos(amount * 1e9)}
            </SelectItem>
          ))}
          <SelectItem value="custom">Custom amount</SelectItem>
        </SelectContent>
      </Select>
      {selection === "custom" ? (
        <Input
          aria-label={`${label}, custom amount`}
          inputMode="decimal"
          placeholder="Enter amount"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : null}
    </div>
  );
}
