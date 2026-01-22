import { forwardRef } from "react";
import {
  Item,
  ItemContent,
  ItemDescription,
  ItemMedia,
  ItemTitle,
} from "@/components/ui/item";
import { cn } from "@/lib/utils";

interface StyledItemProps
  extends Omit<React.ComponentPropsWithoutRef<typeof Item>, "variant"> {
  variant?: "default" | "feature";
}

/**
 * StyledItem - A pre-styled Item component with consistent design across the app
 *
 * Design tokens used:
 * - Border: border-border/60 (standard), hover:border-lazycloud/30 (accent)
 * - Background: bg-muted/60 with backdrop-blur-sm
 * - Radius: rounded-lg
 * - Shadow: shadow-sm (base), hover:shadow-md (feature)
 *
 * @variant default - Standard item with subtle styling
 * @variant feature - Feature card styling with hover effect
 */
export const StyledItem = forwardRef<HTMLDivElement, StyledItemProps>(
  ({ className, variant: styledVariant = "default", size, ...props }, ref) => {
    const itemSize = size ?? (styledVariant === "feature" ? "sm" : undefined);

    return (
      <Item
        ref={ref}
        variant="outline"
        size={itemSize}
        className={cn(
          // Base styles for all variants (matches TUI dashboard tab triggers)
          "rounded-lg border border-border/60 bg-muted/60 shadow-sm backdrop-blur-sm",
          // Variant-specific styles
          styledVariant === "feature" && [
            "cursor-pointer transition-all duration-300 hover:-translate-y-0.5 hover:border-lazycloud/30 hover:shadow-md",
          ],
          className,
        )}
        {...props}
      />
    );
  },
);
StyledItem.displayName = "StyledItem";

export const StyledItemMedia = ItemMedia;
export const StyledItemContent = ItemContent;
export const StyledItemTitle = ItemTitle;
export const StyledItemDescription = ItemDescription;
