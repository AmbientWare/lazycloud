import { Button } from "@/components/ui/button";
import { Wand2 } from "lucide-react";
import type { ReactNode } from "react";

interface HeaderProps {
  action?: ReactNode;
}

export function Header({ action }: HeaderProps) {
  return (
    <header className="border-b bg-background/80 backdrop-blur-sm sticky top-0 z-10">
      <div className="container max-w-5xl mx-auto px-4 h-16 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="h-9 w-9 rounded-lg bg-primary flex items-center justify-center">
            <Wand2 className="h-5 w-5 text-primary-foreground" />
          </div>
          <span className="font-semibold text-lg">Image Transformer</span>
        </div>
        {action}
      </div>
    </header>
  );
}

interface PageLayoutProps {
  children: ReactNode;
  action?: ReactNode;
  maxWidth?: "3xl" | "4xl" | "5xl" | "6xl" | "7xl";
}

export function PageLayout({ children, action, maxWidth = "5xl" }: PageLayoutProps) {
  const maxWidthClass = {
    "3xl": "max-w-3xl",
    "4xl": "max-w-4xl",
    "5xl": "max-w-5xl",
    "6xl": "max-w-6xl",
    "7xl": "max-w-7xl",
  }[maxWidth];

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-muted/30">
      <Header action={action} />
      <main className={`container ${maxWidthClass} mx-auto px-4 py-8`}>
        {children}
      </main>
    </div>
  );
}
