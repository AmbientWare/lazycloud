/** Bounds the pages a live list retains and refetches on each change. */
export const LIVE_LIST_MAX_PAGES = 3;

/** A repeated cursor ends the list rather than fetching the same page forever. */
export function nextListCursor(
  lastPage: { next: string },
  pages: readonly { next: string }[],
): string | undefined {
  if (!lastPage.next) return undefined;
  const repeated = pages.some(
    (page, index) => index < pages.length - 1 && page.next === lastPage.next,
  );
  return repeated ? undefined : lastPage.next;
}

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
