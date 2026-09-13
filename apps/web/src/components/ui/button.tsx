import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type * as React from "react";

import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex h-8 shrink-0 items-center justify-center gap-2 rounded-md border text-sm font-medium outline-none transition-[color,background-color,border-color,box-shadow] focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: "border-primary bg-primary text-primary-foreground hover:bg-lazycloud-light",
        secondary: "border-input bg-secondary text-secondary-foreground hover:bg-accent",
        ghost:
          "border-transparent bg-transparent text-muted-foreground hover:bg-accent hover:text-foreground",
        destructive:
          "border-destructive bg-destructive text-destructive-foreground hover:bg-destructive/90",
        outline: "border-input bg-transparent text-foreground hover:bg-accent",
      },
      size: {
        sm: "h-7 px-2 text-xs",
        md: "h-8 px-3",
        /* Editorial sizes: marketing headers, heroes, and page-level calls to
           action, where a workspace-dense 32px control reads as a control. */
        lg: "h-9 gap-2 px-3.5 text-[13px] font-semibold [&_svg]:size-4.5",
        xl: "h-12 gap-6 px-4 text-sm font-semibold [&_svg]:size-5",
        icon: "size-8 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
);

export type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean;
  };

export function Button({ className, variant, size, asChild = false, ...props }: ButtonProps) {
  const Component = asChild ? Slot : "button";
  return <Component className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}

export { buttonVariants };
