import { useEffect, useId, useRef } from "react";

import "./runModeArt.css";

type RunMode = "local" | "cloud" | "production";

function Definition({ x, y, small = false }: { x: number; y: number; small?: boolean }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${small ? 0.65 : 1})`}>
      <circle className="run-art-core-ring" r="13" />
      <path
        className="run-art-brackets"
        d="M-11-20h-5q-5 0-5 5v7q0 8-7 8 7 0 7 8v7q0 5 5 5h5M11-20h5q5 0 5 5v7q0 8 7 8-7 0-7 8v7q0 5-5 5h-5"
      />
      <circle className="run-art-core" r="3" />
      <path className="run-art-core-ticks" d="M0-8v-2M0 8v2M-8 0h-2M8 0h2" />
    </g>
  );
}

function Signal({ d, phase = 0 }: { d: string; phase?: number }) {
  const style = { animationDelay: `calc(var(--run-delay) + ${phase}s)` };
  return (
    <>
      <path className="run-art-route" d={d} />
      <path className="run-art-signal" d={d} pathLength="100" style={style} />
      <path className="run-art-signal run-art-signal-tip" d={d} pathLength="100" style={style} />
    </>
  );
}

export function RunModeArt({ mode }: { mode: RunMode }) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    let visible = false;
    const sync = () => {
      element.dataset.playing = String(visible && !document.hidden);
    };
    const observer = new IntersectionObserver(
      ([entry]) => {
        visible = entry.isIntersecting;
        sync();
      },
      { threshold: 0.2 },
    );
    observer.observe(element);
    document.addEventListener("visibilitychange", sync);
    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", sync);
    };
  }, []);

  return (
    <div className={`run-art run-art-${mode}`} ref={ref} data-playing="false" aria-hidden="true">
      <svg viewBox="0 0 360 210" fill="none">
        <defs>
          <radialGradient
            id={`${id}-ink`}
            gradientUnits="userSpaceOnUse"
            cx={mode === "production" ? 70 : 140}
            cy="65"
            r={mode === "production" ? 110 : 160}
          >
            <stop stopColor="#e1f6ff" stopOpacity="0.9" />
            <stop offset="0.55" stopColor="#8ccbdc" stopOpacity="0.55" />
            <stop offset="1" stopColor="#54869c" stopOpacity="0.15" />
          </radialGradient>
          <linearGradient
            id={`${id}-syntax`}
            x1="100"
            y1="60"
            x2="210"
            y2="155"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="#d4e8ee" />
            <stop offset="1" stopColor="#6e899a" stopOpacity="0.45" />
          </linearGradient>
          <linearGradient
            id={`${id}-edge`}
            x1="100"
            y1="40"
            x2="200"
            y2="150"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="#bfeaff" stopOpacity="0.45" />
            <stop offset="1" stopColor="#6fd4ed" stopOpacity="0.02" />
          </linearGradient>
          <clipPath id={`${id}-cloud`}>
            <path d="M99 150a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z" />
          </clipPath>
        </defs>

        {mode === "local" && (
          <>
            <g
              className="run-art-code"
              stroke={`url(#${id}-syntax)`}
              strokeLinecap="round"
              strokeWidth="3"
            >
              <path d="M133 63h49m9 0h30M115 83h39m9 0h54M144 103h37m9 0h37M115 123h57m9 0h22M136 143h47" />
              <path className="run-art-syntax-accent" d="M102 63h22M115 103h20M102 143h25" />
            </g>
            <path className="run-art-indent" d="M102 76v54" />
            <g className="run-art-gutter" fill="currentColor">
              {[63, 103, 123, 143].map((y) => (
                <circle key={y} cx="88" cy={y} r="1.2" />
              ))}
            </g>
            <g className="run-art-debugger">
              <path d="M98 82h136" stroke="currentColor" strokeWidth="18" opacity="0.07" />
              <circle cx="88" cy="82" r="3.5" fill="currentColor" />
              <circle className="run-art-breakpoint-ring" cx="88" cy="82" r="7" />
              <path d="m239 78 5 4-5 4" stroke="currentColor" strokeWidth="1.5" />
            </g>
            <Signal d="M74 63H58q-13 0-13 13v73q0 20 20 20h200q25 0 25-25V89q0-26-26-26h-17" />
            <g className="run-art-route-dots" fill={`url(#${id}-ink)`}>
              {Array.from({ length: 18 }, (_, i) => (
                <circle key={i} cx={76 + i * 10} cy="181" r="0.85" />
              ))}
            </g>
            <Definition x={260} y={126} small />
            <path className="run-art-pointer" d="m164 106 2 25 7-8 10-2Z" />
          </>
        )}

        {mode === "cloud" && (
          <>
            <path
              d="M99 155a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z"
              stroke={`url(#${id}-edge)`}
              strokeWidth="0.75"
            />
            <g clipPath={`url(#${id}-cloud)`} fill={`url(#${id}-ink)`}>
              <path
                d="M99 150a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z"
                fill="var(--brand)"
                opacity="0.035"
              />
              {Array.from({ length: 26 }, (_, x) =>
                Array.from({ length: 16 }, (_, y) =>
                  Math.hypot(64 + x * 8 - 176, 32 + y * 8 - 100) > 35 ? (
                    <circle
                      key={`${x}-${y}`}
                      cx={64 + x * 8}
                      cy={32 + y * 8}
                      r={0.75 + 0.75 * Math.max(0, 1 - Math.hypot(x - 10, y - 5) / 17)}
                    />
                  ) : null,
                ),
              )}
              <path
                className="run-art-scan"
                d="M75 32v124"
                stroke="var(--brand)"
                strokeWidth="18"
                opacity="0.2"
              />
            </g>
            <path
              d="M99 150a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z"
              stroke={`url(#${id}-edge)`}
              strokeWidth="0.75"
            />
            <Signal d="M30 107h73q16 0 16 16v35q0 17 17 17h70q16 0 16-16v-19" />
            <Signal d="M207 99h90q19 0 19 19v40q0 17-17 17h-41" phase={0.8} />
            <Definition x={176} y={100} />
            <circle className="run-art-terminal" cx="30" cy="107" r="4" />
            <g className="run-art-result">
              <circle className="run-art-result-ring" cx="256" cy="174" r="13" />
              <path className="run-art-check" d="m250 172 5 5 9-10" />
            </g>
          </>
        )}

        {mode === "production" && (
          <>
            <Signal d="M115 105h46q19 0 19-19V65q0-20 20-20h44" />
            <Signal d="M115 105h129" phase={0.2} />
            <Signal d="M115 105h46q19 0 19 19v21q0 20 20 20h44" phase={0.4} />
            <g fill={`url(#${id}-ink)`}>
              {Array.from({ length: 11 }, (_, x) =>
                Array.from({ length: 11 }, (_, y) => {
                  const distance = Math.hypot(x - 5, y - 5);
                  return distance > 3.5 && distance < 5.8 ? (
                    <circle key={`${x}-${y}`} cx={34 + x * 10} cy={55 + y * 10} r="1" />
                  ) : null;
                }),
              )}
            </g>
            <path
              className="run-art-source-orbit"
              d="M60 67a45 45 0 0 1 47 0M108 143a45 45 0 0 1-47 0"
            />
            <Definition x={84} y={105} />
            <g className="run-art-node-rings">
              {[45, 105, 165].map((y) => (
                <path key={y} d={`M259 ${y - 23}a25 25 0 0 1 33 33m-13 13a25 25 0 0 1-33-33`} />
              ))}
            </g>
            <g className="run-art-destinations">
              <path d="m260 38-8 7 8 7m17-14 8 7-8 7m-6-17-5 20" />
              <path d="m262 95-10 10 10 10m14-20 10 10-10 10" />
              <circle cx="269" cy="165" r="13" />
              <path d="M269 157v8l6 4" />
            </g>
            <g className="run-art-online" fill="currentColor">
              <circle cx="310" cy="45" r="2.5" />
              <circle cx="310" cy="105" r="2.5" />
              <circle cx="310" cy="165" r="2.5" />
            </g>
          </>
        )}
      </svg>
    </div>
  );
}
