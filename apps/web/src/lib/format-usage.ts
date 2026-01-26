export function formatUsageValue(value: number): string {
  if (value <= 0) {
    return '0.00'
  }
  if (value < 0.01) {
    return '0.01'
  }
  return value.toFixed(2)
}
