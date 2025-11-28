interface GridBackgroundProps {
  children?: React.ReactNode;
}

export function GridBackground({ children }: GridBackgroundProps) {
  return (
    <div className="relative min-h-screen w-full">
      <div className="absolute inset-0 -z-10 h-full w-full overflow-hidden">
        <svg className="h-full w-full">
          <defs>
            <linearGradient
              id="grid-gradient"
              x1="0%"
              y1="0%"
              x2="100%"
              y2="100%"
            >
              <stop
                offset="0%"
                stopColor="currentColor"
                stopOpacity="0.08"
              />
              <stop
                offset="50%"
                stopColor="currentColor"
                stopOpacity="0.04"
              />
              <stop
                offset="100%"
                stopColor="currentColor"
                stopOpacity="0.02"
              />
            </linearGradient>
            <radialGradient
              id="grid-radial"
              cx="50%"
              cy="50%"
              r="50%"
            >
              <stop
                offset="0%"
                stopColor="currentColor"
                stopOpacity="0.06"
              />
              <stop
                offset="100%"
                stopColor="currentColor"
                stopOpacity="0"
              />
            </radialGradient>
          </defs>
          <rect
            width="100%"
            height="100%"
            fill="url(#grid-gradient)"
            className="text-muted-foreground"
          />
          <rect
            width="100%"
            height="100%"
            fill="url(#grid-radial)"
            className="text-muted-foreground opacity-50"
          />
          <pattern
            id="grid"
            width="50"
            height="50"
            patternUnits="userSpaceOnUse"
          >
            <path
              d="M 50 0 L 0 0 0 50"
              fill="none"
              stroke="currentColor"
              strokeWidth="0.75"
              strokeOpacity="0.25"
              className="text-muted-foreground"
            />
          </pattern>
          <rect
            width="100%"
            height="100%"
            fill="url(#grid)"
            className="text-muted-foreground"
          />
        </svg>
      </div>
      {children}
    </div>
  );
}
