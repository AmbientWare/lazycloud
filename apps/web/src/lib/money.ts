/**
 * Nanodollars, which is how every figure the platform bills is stored and sent.
 *
 * Billionths of a dollar, so a second of even the cheapest resource carries
 * an exact price rather than a rounded one. Rendering is where rounding is
 * allowed to happen, and only there.
 */
export const NANOS_PER_DOLLAR = 1_000_000_000;

/**
 * A charge, in the reader's own locale.
 *
 * Small amounts keep more decimals than a currency normally shows: an account
 * that has spent a third of a cent has spent something, and rendering it as
 * $0.00 would say it has not.
 */
export function formatCostNanos(nanos: number, currency = "USD"): string {
  const dollars = nanos / NANOS_PER_DOLLAR;
  const magnitude = Math.abs(dollars);
  return Intl.NumberFormat(undefined, {
    style: "currency",
    currency,
    currencyDisplay: "narrowSymbol",
    minimumFractionDigits: 2,
    maximumFractionDigits: magnitude > 0 && magnitude < 0.01 ? 6 : 2,
  }).format(dollars);
}

/**
 * A published rate, carried out exactly.
 *
 * Integer arithmetic on the stored nanodollars rather than floating point, so a
 * price on the page is the price in the rate row and not a rounding of it. A
 * figure that cannot be written exactly would be a different price, which is the
 * one thing a rate card may not do.
 */
export function exactDollars(nanos: number): string {
  const sign = nanos < 0 ? "-" : "";
  const magnitude = Math.abs(nanos);
  const whole = Math.floor(magnitude / NANOS_PER_DOLLAR);
  const fraction = String(magnitude % NANOS_PER_DOLLAR)
    .padStart(9, "0")
    .replace(/0+$/, "");
  if (!fraction) return `${sign}$${whole}`;
  return `${sign}$${whole}.${fraction.length < 2 ? fraction.padEnd(2, "0") : fraction}`;
}
