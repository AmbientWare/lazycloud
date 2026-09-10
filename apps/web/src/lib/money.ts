const NANOS_PER_DOLLAR = 1_000_000_000;

export function formatCostNanos(nanos: number, currency = "USD"): string {
  const dollars = nanos / NANOS_PER_DOLLAR;
  const formatter = Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  // A nonzero sub-cent amount must remain distinguishable from free usage.
  if (dollars !== 0 && Math.abs(dollars) < 0.01) {
    return dollars > 0 ? `<${formatter.format(0.01)}` : `>${formatter.format(-0.01)}`;
  }
  return formatter.format(dollars);
}
