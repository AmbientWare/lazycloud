import { useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Trash2 } from "lucide-react";

import { ConfirmButton } from "@/components/shared/ConfirmButton";
import { FeedbackKindIcon } from "@/components/shared/FeedbackKindIcon";
import { InfiniteScrollBoundary } from "@/components/shared/InfiniteScrollBoundary";
import { Panel } from "@/components/shared/Panel";
import { PanelEmpty } from "@/components/shared/PanelEmpty";
import { PanelError } from "@/components/shared/PanelError";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError, type Schemas } from "@/lib/api/client";
import { exactTime, relativeTime } from "@/lib/format";
import { deleteFeedback, FEEDBACK_KINDS, feedbackQueryOptions } from "@/lib/queries/feedback";
import { accountQueryKeys } from "@/lib/queries/workspace-keys";
import { cn } from "@/lib/utils";

import { SEARCH_DEBOUNCE_MS, useDebounced } from "./controller";

type Feedback = Schemas["Feedback"];
type Kind = Schemas["FeedbackKind"];

const FILTERS: { value: Kind | null; label: string }[] = [
  { value: null, label: "All" },
  { value: "feature", label: "Features" },
  { value: "bug", label: "Bugs" },
  { value: "support", label: "Support" },
];

function authorName(author: Schemas["User"]): string {
  return author.display_name || author.github_login || author.email;
}

function kindLabel(kind: Kind): string {
  return FEEDBACK_KINDS.find((option) => option.value === kind)?.label ?? kind;
}

/** What every account has sent from the Feedback dialog, newest first. */
export function FeedbackSettings() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<Kind | null>(null);
  const [search, setSearch] = useState("");
  const settledSearch = useDebounced(search.trim(), SEARCH_DEBOUNCE_MS);
  const query = useInfiniteQuery(feedbackQueryOptions({ kind, search: settledSearch }));
  const [openId, setOpenId] = useState<string | null>(null);
  const remove = useMutation({
    mutationFn: deleteFeedback,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: accountQueryKeys.admin.feedback.root() }),
  });
  const items = query.data?.pages.flatMap((page) => page.feedback) ?? [];
  const forbidden = query.error instanceof ApiError && query.error.status === 403;
  const narrowed = Boolean(kind || search);

  return (
    <Panel
      pending={query.isPending}
      title="Feedback"
      className="min-h-0 flex-1"
      contentClassName="flex flex-col overflow-hidden"
    >
      {forbidden ? null : (
        <div className="flex shrink-0 flex-col gap-2 border-b border-border px-4 py-3 sm:flex-row sm:items-center">
          <Input
            aria-label="Search feedback"
            className="sm:flex-1"
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search messages, names, or emails"
            value={search}
          />
          <div
            role="group"
            aria-label="Type"
            className="flex items-center gap-0.5 self-start rounded-md border border-input bg-card p-0.5 sm:self-auto"
          >
            {FILTERS.map((filter) => {
              const selected = filter.value === kind;
              return (
                <button
                  key={filter.label}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => setKind(filter.value)}
                  className={cn(
                    "flex h-7 items-center gap-1.5 rounded-xs px-2.5 text-xs whitespace-nowrap outline-none transition-colors",
                    "focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset",
                    selected
                      ? "bg-accent font-medium text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {filter.value ? (
                    <FeedbackKindIcon kind={filter.value} className="size-3.5" />
                  ) : null}
                  {filter.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-y-auto">
        {query.isPending ? (
          <FeedbackSkeleton />
        ) : forbidden ? (
          <PanelError message="Only an administrator can see feedback." />
        ) : query.isError && !query.data ? (
          <PanelError message={query.error.message} />
        ) : items.length === 0 ? (
          <PanelEmpty
            message={narrowed ? "No feedback matches these filters" : "No feedback yet"}
            className="min-h-full p-4"
          />
        ) : (
          <div>
            <ul className="divide-y divide-border/70">
              {items.map((item) => (
                <FeedbackItem
                  key={item.id}
                  item={item}
                  open={openId === item.id}
                  onToggle={() => setOpenId(openId === item.id ? null : item.id)}
                  deleting={remove.isPending && remove.variables === item.id}
                  deleteError={
                    remove.isError && remove.variables === item.id ? remove.error.message : null
                  }
                  onDelete={() => remove.mutate(item.id)}
                />
              ))}
            </ul>
            <InfiniteScrollBoundary
              nextCursor={query.hasNextPage ? query.data?.pages.at(-1)?.next_cursor : undefined}
              loading={query.isFetchingNextPage}
              error={query.isFetchNextPageError}
              onLoadMore={() => void query.fetchNextPage()}
              resourceLabel="feedback"
            />
          </div>
        )}
      </div>
    </Panel>
  );
}

function FeedbackItem({
  item,
  open,
  onToggle,
  deleting,
  deleteError,
  onDelete,
}: {
  item: Feedback;
  open: boolean;
  onToggle: () => void;
  deleting: boolean;
  deleteError: string | null;
  onDelete: () => void;
}) {
  const { author } = item;
  const name = authorName(author);
  return (
    <li className={cn(open && "bg-background/35")}>
      <button
        type="button"
        aria-expanded={open}
        data-selected={open}
        onClick={onToggle}
        className="interactive-row group flex w-full min-w-0 items-center gap-3 px-3 py-3 text-left"
      >
        <ChevronRight
          aria-hidden="true"
          className={cn(
            "interactive-row-indicator size-3.5 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none",
            open && "rotate-90 text-brand",
          )}
        />
        <span title={kindLabel(item.kind)} className="shrink-0 text-muted-foreground">
          <FeedbackKindIcon kind={item.kind} className="size-4" />
          <span className="sr-only">{kindLabel(item.kind)}</span>
        </span>
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[13px]",
            open ? "text-muted-foreground" : "text-foreground",
          )}
        >
          {item.message}
        </span>
        <span className="hidden max-w-40 shrink-0 truncate text-xs text-muted-foreground sm:block">
          {name}
        </span>
        <span
          className="w-24 shrink-0 text-right text-xs text-muted-foreground"
          title={exactTime(item.created_at)}
        >
          {relativeTime(item.created_at)}
        </span>
      </button>
      {open ? (
        <div className="flex gap-3 pt-1 pr-3 pb-4 pl-[4.125rem]">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Avatar author={author} />
              <span className="truncate text-foreground">{name}</span>
              {author.email && author.email !== name ? (
                <span className="truncate">{author.email}</span>
              ) : null}
            </div>
            <p className="mt-3 max-w-prose whitespace-pre-wrap break-words text-sm leading-relaxed">
              {item.message}
            </p>
            {deleteError ? (
              <p role="alert" className="mt-3 text-xs text-destructive">
                {deleteError}
              </p>
            ) : null}
          </div>
          <ConfirmButton
            label="Delete feedback"
            icon={Trash2}
            pending={deleting}
            onConfirm={onDelete}
          />
        </div>
      ) : null}
    </li>
  );
}

function Avatar({ author }: { author: Schemas["User"] }) {
  if (author.avatar_url) {
    return <img src={author.avatar_url} alt="" className="size-5 shrink-0 rounded-md" />;
  }
  return (
    <span
      aria-hidden="true"
      className="flex size-5 shrink-0 items-center justify-center rounded-md bg-muted text-[10px] font-medium text-muted-foreground"
    >
      {authorName(author).slice(0, 1).toUpperCase()}
    </span>
  );
}

function FeedbackSkeleton() {
  return (
    <div className="space-y-3 p-4">
      {[0, 1, 2].map((item) => (
        <Skeleton key={item} className="h-9 w-full" />
      ))}
    </div>
  );
}
