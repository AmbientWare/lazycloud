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
 * @variant default - Standard item with subtle styling (size="sm", rounded-lg)
 * @variant feature - Feature card styling with enhanced shadows and backdrop blur
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
          styledVariant === "default" &&
            "border-border/70 bg-muted/60 backdrop-blur-sm rounded-lg shadow-sm shadow-black/30 dark:shadow-white/10",
          styledVariant === "feature" && [
            "border-border/70 bg-muted/60 backdrop-blur-sm rounded-lg shadow-sm shadow-black/35 dark:shadow-white/12",
            "transition-all duration-300 hover:-translate-y-0.5 hover:shadow-md hover:shadow-black/45 dark:hover:shadow-white/18 hover:border-lazycloud/30",
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
