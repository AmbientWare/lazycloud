/** Bounds the pages a live list retains and refetches on each change. */
export const LIVE_LIST_MAX_PAGES = 3;

/** A page of any API collection: its items under a named key, then `next_cursor`. */
export type CursorPage = { readonly next_cursor?: string };

/**
 * The cursor of the page after `lastPage`. A repeated cursor ends the list rather
 * than fetching the same page forever.
 */
export function nextPageCursor(
  lastPage: CursorPage,
  pages: readonly CursorPage[],
): string | undefined {
  const next = lastPage.next_cursor;
  if (!next) return undefined;
  const repeated = pages.some(
    (page, index) => index < pages.length - 1 && page.next_cursor === next,
  );
  return repeated ? undefined : next;
}

export type InfiniteListSelection<TItem> = {
  items: TItem[];
  nextCursor: string | undefined;
};

/** Flatten TanStack's page collection into one list, deduplicated by `itemKey`. */
export function selectPages<TPage extends CursorPage, TItem>(
  data: { readonly pages: readonly TPage[] } | undefined,
  items: (page: TPage) => readonly TItem[],
  hasNextPage: boolean | undefined,
  itemKey?: (item: TItem) => string,
): InfiniteListSelection<TItem> {
  const result: TItem[] = [];
  const seen = itemKey ? new Set<string>() : undefined;
  for (const page of data?.pages ?? []) {
    for (const item of items(page)) {
      if (itemKey && seen) {
        const key = itemKey(item);
        if (seen.has(key)) continue;
        seen.add(key);
      }
      result.push(item);
    }
  }
  return {
    items: result,
    nextCursor: hasNextPage ? data?.pages.at(-1)?.next_cursor || undefined : undefined,
  };
}

/* The envelope of collections that have not moved to the public API yet. */

export function nextListCursor(
  lastPage: { next: string },
  pages: readonly { next: string }[],
): string | undefined {
  return nextPageCursor(
    { next_cursor: lastPage.next },
    pages.map((page) => ({ next_cursor: page.next })),
  );
}

export type InfiniteListQueryData<TItem> = {
  readonly pages: readonly {
    readonly data: readonly TItem[];
    readonly next: string;
  }[];
};

export function selectInfiniteList<TItem>(
  data: InfiniteListQueryData<TItem> | undefined,
  hasNextPage: boolean | undefined,
  itemKey?: (item: TItem) => string,
): InfiniteListSelection<TItem> {
  return selectPages(
    data && { pages: data.pages.map((page) => ({ data: page.data, next_cursor: page.next })) },
    (page) => page.data,
    hasNextPage,
    itemKey,
  );
}
