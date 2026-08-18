/** `12 apps`, `1 app` — the count first, because that is what is being scanned. */
export function countLabel(value: number, singular: string, plural = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}
