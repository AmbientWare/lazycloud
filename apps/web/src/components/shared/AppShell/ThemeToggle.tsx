import { Moon, Sun } from "lucide-react";

import { useTheme } from "@/components/shared/ThemeProvider/theme";
import { cn } from "@/lib/utils";

/** Shell theme control: switches between the light default and dark mode. */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  const Icon = theme === "dark" ? Sun : Moon;
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      aria-label={`Switch to ${next} mode`}
      title={`Switch to ${next} mode`}
      className={cn(
        "interactive-row flex h-9 w-full items-center gap-2.5 rounded-md px-2.5 text-[13px] text-muted-foreground hover:text-foreground",
        className,
      )}
    >
      <Icon className="size-4" aria-hidden="true" />
      {theme === "dark" ? "Light mode" : "Dark mode"}
    </button>
  );
}
