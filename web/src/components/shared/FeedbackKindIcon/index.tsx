import { Bug, LifeBuoy, Lightbulb, type LucideProps } from "lucide-react";

import type { Schemas } from "@/lib/api/client";

const ICONS = { feature: Lightbulb, bug: Bug, support: LifeBuoy } as const;

/** The mark a kind of feedback carries in the dialog and the admin list. */
export function FeedbackKindIcon({
  kind,
  ...props
}: { kind: Schemas["FeedbackKind"] } & LucideProps) {
  const Icon = ICONS[kind];
  return <Icon aria-hidden="true" {...props} />;
}
