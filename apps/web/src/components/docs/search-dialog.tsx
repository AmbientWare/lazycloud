"use client";

import { useDocsSearch } from "fumadocs-core/search/client";
import {
  SearchDialog,
  SearchDialogClose,
  SearchDialogContent,
  SearchDialogHeader,
  SearchDialogIcon,
  SearchDialogInput,
  SearchDialogList,
  SearchDialogOverlay,
  type SharedProps,
} from "fumadocs-ui/components/dialog/search";
import { useSearchContext } from "fumadocs-ui/contexts/search";
import { Button } from "@/components/ui/button";
import { useIsMac } from "@/hooks/use-is-mac";

export default function CustomSearchDialog(props: SharedProps) {
  const { search, setSearch, query } = useDocsSearch({
    type: "fetch",
    api: "/api/search",
  });

  return (
    <SearchDialog
      search={search}
      onSearchChange={setSearch}
      isLoading={query.isLoading}
      {...props}
    >
      <SearchDialogOverlay />
      <SearchDialogContent>
        <SearchDialogHeader>
          <SearchDialogIcon />
          <SearchDialogInput />
          <SearchDialogClose />
        </SearchDialogHeader>
        <SearchDialogList items={query.data !== "empty" ? query.data : null} />
      </SearchDialogContent>
    </SearchDialog>
  );
}

export function DocsSearch() {
  const { setOpenSearch } = useSearchContext();
  const isMac = useIsMac();

  return (
    <Button
      variant="outline"
      className="text-muted-foreground relative h-9 w-full justify-start rounded-md text-sm pr-12"
      onClick={() => setOpenSearch(true)}
    >
      <span className="truncate">Search...</span>
      <kbd className="bg-muted pointer-events-none absolute right-1.5 top-1.5 hidden h-6 select-none items-center gap-1 rounded border px-1.5 font-mono text-[10px] font-medium opacity-100 sm:flex">
        <span className="text-xs">{isMac ? "⌘" : "Ctrl"}</span>K
      </kbd>
    </Button>
  );
}
