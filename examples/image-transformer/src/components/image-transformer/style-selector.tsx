import { Sparkles, Palette, Brush, Zap, Grid3X3, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { STYLES, type StyleKey } from "@/lib/replicate";

interface StyleSelectorProps {
  value: StyleKey;
  onChange: (style: StyleKey) => void;
  disabled?: boolean;
  variant?: "grid" | "compact";
}

const STYLE_CONFIG: Record<StyleKey, { icon: React.ReactNode; description: string }> = {
  ghibli: {
    icon: <Sparkles className="h-5 w-5" />,
    description: "Soft, hand-drawn anime aesthetic",
  },
  pixar: {
    icon: <Palette className="h-5 w-5" />,
    description: "Vibrant 3D animated style",
  },
  watercolor: {
    icon: <Brush className="h-5 w-5" />,
    description: "Soft, flowing brushstrokes",
  },
  comic: {
    icon: <Zap className="h-5 w-5" />,
    description: "Bold lines, cel shading",
  },
  pixel: {
    icon: <Grid3X3 className="h-5 w-5" />,
    description: "Retro 16-bit game aesthetic",
  },
};

export function StyleSelector({ value, onChange, disabled, variant = "grid" }: StyleSelectorProps) {
  const styles = Object.keys(STYLES) as StyleKey[];

  if (variant === "compact") {
    return (
      <div className="flex flex-wrap gap-2">
        {styles.map((key) => {
          const config = STYLE_CONFIG[key];
          const isSelected = value === key;

          return (
            <button
              key={key}
              type="button"
              disabled={disabled}
              onClick={() => onChange(key)}
              className={cn(
                "flex items-center gap-2 px-4 py-2 rounded-full border text-sm font-medium transition-colors",
                isSelected
                  ? "bg-primary text-primary-foreground border-primary"
                  : "bg-background hover:bg-accent border-input",
                disabled && "opacity-50 cursor-not-allowed"
              )}
            >
              {config.icon}
              {STYLES[key].name}
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-2">
      {styles.map((key) => {
        const config = STYLE_CONFIG[key];
        const isSelected = value === key;

        return (
          <button
            key={key}
            type="button"
            disabled={disabled}
            onClick={() => onChange(key)}
            className={cn(
              "flex items-center gap-3 p-3 rounded-lg border text-left transition-colors",
              isSelected
                ? "border-primary bg-primary/5"
                : "border-input hover:bg-accent",
              disabled && "opacity-50 cursor-not-allowed"
            )}
          >
            <div
              className={cn(
                "flex h-10 w-10 shrink-0 items-center justify-center rounded-lg",
                isSelected
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground"
              )}
            >
              {config.icon}
            </div>

            <div className="flex-1 min-w-0">
              <p className="font-medium">{STYLES[key].name}</p>
              <p className="text-xs text-muted-foreground">{config.description}</p>
            </div>

            {isSelected && (
              <div className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                <Check className="h-3 w-3" />
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
