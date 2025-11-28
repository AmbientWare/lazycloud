import { forwardRef } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface StyledCardProps extends React.ComponentPropsWithoutRef<typeof Card> {
  variant?: "default" | "interactive" | "minimal" | "elevated" | "static";
  elevation?: 1 | 2 | 3;
}

/**
 * StyledCard - A pre-styled Card component with consistent design across the app
 *
 * @variant default - Standard card with landing page styling (rounded-2xl, shadow)
 * @variant interactive - Default styling + enhanced hover and focus states
 * @variant minimal - Minimal styling, inherits base Card styles (for app UI like lists)
 * @variant elevated - App UI card with shadow and hover effect
 * @variant static - Same as elevated but without hover effect
 */
export const StyledCard = forwardRef<HTMLDivElement, StyledCardProps>(
  ({ className, variant = "default", elevation = 2, ...props }, ref) => {
    const elevationClasses = {
      1: "bg-muted/60 backdrop-blur-md",
      2: "bg-card/90 backdrop-blur-md",
      3: "bg-popover/95 backdrop-blur-md",
    };

    return (
      <Card
        ref={ref}
        className={cn(
          "relative overflow-hidden",
          variant === "default" &&
            "border-border/80 rounded-2xl shadow-sm shadow-black/40 dark:shadow-white/15 transition-all duration-300",
          variant === "interactive" && [
            "border-border/80 rounded-2xl shadow-sm shadow-black/40 dark:shadow-white/15",
            "transition-all duration-300 hover:-translate-y-1 hover:shadow-md hover:shadow-black/50 dark:hover:shadow-white/20 hover:shadow-lazycloud/10 hover:border-lazycloud/30",
            "focus-visible:ring-primary/50 focus-visible:ring-2 focus-visible:outline-none",
            "group",
          ],
          variant === "elevated" && [
            "border-border/75 rounded-xl shadow-sm shadow-black/35 dark:shadow-white/12",
            "transition-all duration-300 hover:shadow-md hover:shadow-black/45 dark:hover:shadow-white/18 hover:shadow-lazycloud/5",
          ],
          variant === "static" && [
            "border-border/75 rounded-xl shadow-sm shadow-black/35 dark:shadow-white/12",
          ],
          variant === "minimal" && [
            "border-border/60 rounded-lg shadow-sm shadow-black/30 dark:shadow-white/10",
          ],
          elevationClasses[elevation],
          className,
        )}
        {...props}
      >
        {variant === "interactive" && (
          <div className="absolute inset-0 bg-gradient-to-br from-lazycloud/0 via-lazycloud/5 to-lazycloud/0 opacity-0 transition-opacity duration-300 group-hover:opacity-100 pointer-events-none" />
        )}
        {props.children}
      </Card>
    );
  },
);
StyledCard.displayName = "StyledCard";

export const StyledCardHeader = CardHeader;
export const StyledCardTitle = CardTitle;
export const StyledCardDescription = CardDescription;
export const StyledCardContent = CardContent;
export const StyledCardFooter = CardFooter;
