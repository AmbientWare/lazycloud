/** How many pages a live list keeps, and therefore refetches on a change.

    A live list shows what is happening now rather than an archive, and every
    page it holds is a request it re-issues whenever the change stream says
    something moved. Unbounded, one app view walked twenty pages of a hundred
    containers and walked them again on each event, which is most of what made
    the dashboard slow. */
export const LIVE_LIST_MAX_PAGES = 3;

export type InfiniteListQueryData<TItem> = {
  readonly pages: readonly {
    readonly data: readonly TItem[];
    readonly next: string;
  }[];
};

export type InfiniteListSelection<TItem> = {
  items: TItem[];
  nextCursor: string | undefined;
};

/** Translate TanStack's page collection and the API list envelope into UI list state. */
export function selectInfiniteList<TItem>(
  data: InfiniteListQueryData<TItem> | undefined,
  hasNextPage: boolean | undefined,
  itemKey?: (item: TItem) => string,
): InfiniteListSelection<TItem> {
  const items: TItem[] = [];
  const seen = itemKey ? new Set<string>() : undefined;

  for (const page of data?.pages ?? []) {
    for (const item of page.data) {
      if (itemKey && seen) {
        const key = itemKey(item);
        if (seen.has(key)) continue;
        seen.add(key);
      }
      items.push(item);
    }
  }

  return {
    items,
    nextCursor: hasNextPage ? data?.pages.at(-1)?.next || undefined : undefined,
  };
}
