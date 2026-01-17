"use client";

import { useState } from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import { format, startOfMonth, endOfMonth, subDays, subMonths } from "date-fns";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandGroup,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";

interface DateRangePickerProps {
  startDate: string;
  endDate: string;
  presetId?: string;
  onDateChange: (start: string, end: string, presetId?: string) => void;
}

type Preset = {
  id: string;
  label: string;
  getValue: () => { from: Date; to: Date };
};

const presets: Preset[] = [
  {
    id: "this-month",
    label: "This Month",
    getValue: () => ({
      from: startOfMonth(new Date()),
      to: new Date(),
    }),
  },
  {
    id: "last-month",
    label: "Last Month",
    getValue: () => {
      const lastMonth = subMonths(new Date(), 1);
      return {
        from: startOfMonth(lastMonth),
        to: endOfMonth(lastMonth),
      };
    },
  },
  {
    id: "last-7-days",
    label: "Last 7 Days",
    getValue: () => ({
      from: subDays(new Date(), 6),
      to: new Date(),
    }),
  },
  {
    id: "last-30-days",
    label: "Last 30 Days",
    getValue: () => ({
      from: subDays(new Date(), 29),
      to: new Date(),
    }),
  },
  {
    id: "last-90-days",
    label: "Last 90 Days",
    getValue: () => ({
      from: subDays(new Date(), 89),
      to: new Date(),
    }),
  },
];

// Convert local date to UTC ISO string for URL
const dateToUrlString = (date: Date, isEnd: boolean): string => {
  const year = date.getFullYear();
  const month = date.getMonth();
  const day = date.getDate();

  if (isEnd) {
    // End of local calendar day
    const localEndOfDay = new Date(year, month, day, 23, 59, 59, 999);
    return localEndOfDay.toISOString();
  } else {
    // Start of local calendar day
    const localMidnight = new Date(year, month, day, 0, 0, 0);
    return localMidnight.toISOString();
  }
};

// Normalize date for comparison (local calendar day)
const normalizeDate = (date: Date): string => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
};

export function DateRangePicker({
  startDate,
  endDate,
  presetId,
  onDateChange,
}: DateRangePickerProps) {
  const [open, setOpen] = useState(false);

  // Parse UTC ISO string to Date for display
  const parseDate = (dateString?: string): Date => {
    if (!dateString) return new Date();
    // If it's an ISO string, parse it directly
    if (dateString.includes("T")) {
      return new Date(dateString);
    }
    // Fallback for old YYYY-MM-DD format
    const parts = dateString.split("-");
    const year = Number(parts[0]);
    const month = Number(parts[1]);
    const day = Number(parts[2]);
    return new Date(year, month - 1, day);
  };

  // Parse UTC ISO strings from props, convert to local dates for comparison
  const currentFrom = startDate ? parseDate(startDate) : startOfMonth(new Date());
  const currentTo = endDate ? parseDate(endDate) : new Date();
  const today = normalizeDate(new Date());

  const findMatchingPreset = (): Preset | undefined => {
    // If presetId is provided, use it directly
    if (presetId) {
      return presets.find((p) => p.id === presetId);
    }

    // Otherwise, try to match by date
    const currentFromNorm = normalizeDate(currentFrom);
    const currentToNorm = normalizeDate(currentTo);
    const dynamicEndIds = new Set(["this-month", "last-7-days", "last-30-days", "last-90-days"]);

    const checkPreset = (preset: Preset): boolean => {
      const range = preset.getValue();
      const presetFromNorm = normalizeDate(range.from);
      const presetToNorm = normalizeDate(range.to);

      if (dynamicEndIds.has(preset.id)) {
        return presetFromNorm === currentFromNorm && currentToNorm === today;
      }
      return presetFromNorm === currentFromNorm && presetToNorm === currentToNorm;
    };

    const thisMonth = presets.find((p) => p.id === "this-month");
    if (thisMonth && checkPreset(thisMonth)) {
      return thisMonth;
    }

    for (let i = presets.length - 1; i >= 0; i--) {
      const preset = presets[i];
      if (preset && preset.id !== "this-month" && checkPreset(preset)) {
        return preset;
      }
    }

    return undefined;
  };

  const selectedPreset = findMatchingPreset();

  const displayLabel = selectedPreset
    ? selectedPreset.label
    : startDate && endDate
      ? `${format(parseDate(startDate), "MMM d, yyyy")} - ${format(parseDate(endDate), "MMM d, yyyy")}`
      : "This Month";

  const handlePresetClick = (preset: Preset) => {
    const range = preset.getValue();
    const start = dateToUrlString(range.from, false);
    const end = dateToUrlString(range.to, true);

    setOpen(false);
    onDateChange(start, end, preset.id);
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className="w-full sm:w-[280px] justify-between"
        >
          {displayLabel}
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] sm:w-[280px] p-0">
        <Command>
          <CommandList>
            <CommandGroup>
              {presets.map((preset) => {
                const isSelected = selectedPreset?.id === preset.id;

                return (
                  <CommandItem
                    key={preset.id}
                    value={preset.id}
                    onSelect={() => handlePresetClick(preset)}
                    className="cursor-pointer"
                  >
                    <Check
                      className={cn("mr-2 h-4 w-4", isSelected ? "opacity-100" : "opacity-0")}
                    />
                    {preset.label}
                  </CommandItem>
                );
              })}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
