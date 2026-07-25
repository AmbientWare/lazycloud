export function regionOptions(
  catalogRegions: readonly string[],
  allowedRegions: readonly string[],
): string[] {
  return [...new Set([...catalogRegions, ...allowedRegions])];
}

export function toggleAllowedRegion(
  allowedRegions: readonly string[],
  region: string,
  defaultRegion: string,
): string[] {
  if (region === defaultRegion && allowedRegions.includes(region)) {
    return [...allowedRegions];
  }
  if (allowedRegions.includes(region)) {
    return allowedRegions.filter((item) => item !== region);
  }
  return [...allowedRegions, region];
}

export function regionsEqual(
  left: readonly string[],
  right: readonly string[],
): boolean {
  return left.length === right.length && left.every((region, index) => region === right[index]);
}
