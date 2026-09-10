const NANOS_PER_DOLLAR = 1_000_000_000;

export function formatCostNanos(
  nanos: number,
  currency = "USD",
  maximumFractionDigits = Math.abs(nanos) > 0 && Math.abs(nanos) < 10_000_000 ? 9 : 2,
): string {
  const dollars = nanos / NANOS_PER_DOLLAR;
  return Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits,
  }).format(dollars);
}
