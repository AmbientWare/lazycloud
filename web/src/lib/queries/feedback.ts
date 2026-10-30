import { infiniteQueryOptions } from "@tanstack/react-query";

import { api, ok, type Schemas } from "@/lib/api/client";
import { accountQueryKeys, type FeedbackScope } from "@/lib/queries/workspace-keys";

/** The longest message the API accepts, in characters. */
export const FEEDBACK_MAX_LENGTH = 2000;

/** What feedback can be about, as the dialog and the admin list name it. */
export const FEEDBACK_KINDS = [
  { value: "feature", label: "Feature request" },
  { value: "bug", label: "Bug" },
  { value: "support", label: "Support" },
] as const satisfies readonly { value: Schemas["FeedbackKind"]; label: string }[];

export function submitFeedback(kind: Schemas["FeedbackKind"], message: string) {
  return ok(api.POST("/v1/feedback", { body: { kind, message } }));
}

export function deleteFeedback(feedback: string) {
  return ok(api.DELETE("/v1/feedback/{feedback}", { params: { path: { feedback } } }));
}

/**
 * Every account's feedback, newest first, narrowed by the server. Admin-only:
 * a member gets 403.
 */
export function feedbackQueryOptions(scope: FeedbackScope) {
  return infiniteQueryOptions({
    queryKey: accountQueryKeys.admin.feedback(scope),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      ok(
        api.GET("/v1/feedback", {
          params: {
            query: {
              limit: 50,
              cursor: pageParam,
              kind: scope.kind ?? undefined,
              search: scope.search || undefined,
            },
          },
        }),
      ),
    getNextPageParam: (page) => page.next_cursor,
  });
}
