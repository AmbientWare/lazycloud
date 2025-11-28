import { forwardRef } from "react";
import { Button, type buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { type VariantProps } from "class-variance-authority";

interface StyledButtonProps
  extends Omit<React.ComponentPropsWithoutRef<typeof Button>, "variant">,
    Omit<VariantProps<typeof buttonVariants>, "variant"> {
  variant?: "default" | "primary" | "outline" | "ghost";
}

/**
 * StyledButton - A pre-styled Button component with consistent design across the app
 *
 * @variant default - Standard button styling
 * @variant primary - Primary action button with lazycloud branding (border, shadow, hover effects)
 * @variant outline - Outlined button with subtle styling
 * @variant ghost - Minimal button with hover effects
 */
export const StyledButton = forwardRef<HTMLButtonElement, StyledButtonProps>(
  ({ className, variant = "default", size, ...props }, ref) => {
    const variantClasses = {
      default: "cursor-pointer",
      primary: [
        "bg-lazycloud hover:bg-lazycloud/80 active:bg-lazycloud/70",
        "border-lazycloud/80 hover:border-lazycloud border-2 font-bold",
        "shadow-sm shadow-black/40 dark:shadow-white/15",
        "hover:shadow-md hover:shadow-black/50 dark:hover:shadow-white/20",
        "hover:-translate-y-0.5 active:translate-y-0",
        "transition-all duration-300",
        "cursor-pointer",
        "group",
      ],
      outline: [
        "border-border/70 hover:border-border",
        "bg-background hover:bg-accent/50",
        "shadow-sm shadow-black/30 dark:shadow-white/10",
        "hover:shadow-md hover:shadow-black/40 dark:hover:shadow-white/15",
        "hover:-translate-y-0.5 active:translate-y-0",
        "cursor-pointer",
      ],
      ghost: [
        "hover:bg-accent/50 active:bg-accent/70",
        "transition-all duration-300",
        "cursor-pointer",
      ],
    };

    const baseVariant = variant === "primary" ? "default" : variant === "outline" ? "outline" : variant === "ghost" ? "ghost" : "default";

    return (
      <Button
        ref={ref}
        variant={baseVariant}
        size={size}
        className={cn(
          variantClasses[variant],
          className,
        )}
        {...props}
      />
    );
  },
);
StyledButton.displayName = "StyledButton";

