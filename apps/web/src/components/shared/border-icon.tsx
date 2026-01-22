import { cn } from "@/lib/utils";

/**
 * BorderIcon - A consistent icon wrapper with subtle brand tint
 *
 * Design tokens used:
 * - Border: border-border/60 (matches design system)
 * - Background: bg-lazycloud/10 (subtle brand tint)
 * - Radius: rounded-md
 */
export default function BorderIcon({
  icon,
  className,
}: {
  icon: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-md border border-border/60 bg-lazycloud/10 p-2",
        className,
      )}
    >
      {icon}
    </div>
  );
}
