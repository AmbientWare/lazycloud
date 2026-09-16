import { useEffect, useId, useRef } from "react";

import "./runModeArt.css";

type RunMode = "local" | "cloud" | "production";

function Definition({ x, y, small = false }: { x: number; y: number; small?: boolean }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${small ? 0.65 : 1})`}>
      <path
        className="run-art-brackets"
        d="M-11-20h-5q-5 0-5 5v7q0 8-7 8 7 0 7 8v7q0 5 5 5h5M11-20h5q5 0 5 5v7q0 8 7 8-7 0-7 8v7q0 5-5 5h-5"
      />
      <circle className="run-art-core" r="3" />
    </g>
  );
}

function Signal({ d }: { d: string }) {
  return (
    <>
      <path className="run-art-route" d={d} />
      <path className="run-art-signal" d={d} pathLength="100" />
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
          <radialGradient id={`${id}-ink`}>
            <stop stopColor="#c9eaf5" stopOpacity="0.75" />
            <stop offset="1" stopColor="#83acc0" stopOpacity="0.12" />
          </radialGradient>
          <clipPath id={`${id}-cloud`}>
            <path d="M99 150a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z" />
          </clipPath>
        </defs>

        {mode === "local" && (
          <>
            <g className="run-art-code" strokeLinecap="round" strokeWidth="3">
              <path d="M102 63h22m9 0h49m9 0h30M115 83h39m9 0h54M115 103h20m9 0h37m9 0h37M115 123h57m9 0h22M102 143h25m9 0h47" />
            </g>
            <g className="run-art-debugger">
              <path d="M98 82h136" stroke="currentColor" strokeWidth="18" opacity="0.07" />
              <circle cx="88" cy="82" r="3.5" fill="currentColor" />
              <path d="m239 78 5 4-5 4" stroke="currentColor" strokeWidth="1.5" />
            </g>
            <Signal d="M74 63H58q-13 0-13 13v73q0 20 20 20h200q25 0 25-25V89q0-26-26-26h-17" />
            <Definition x={260} y={126} small />
            <path className="run-art-pointer" d="m164 106 2 25 7-8 10-2Z" />
          </>
        )}

        {mode === "cloud" && (
          <>
            <g clipPath={`url(#${id}-cloud)`} fill={`url(#${id}-ink)`}>
              <path
                d="M99 150a33 33 0 0 1-5-65 44 44 0 0 1 79-29 35 35 0 0 1 62 16 40 40 0 0 1 0 78Z"
                fill="var(--brand)"
                opacity="0.025"
              />
              {Array.from({ length: 26 }, (_, x) =>
                Array.from({ length: 16 }, (_, y) =>
                  Math.hypot(64 + x * 8 - 176, 32 + y * 8 - 100) > 35 ? (
                    <circle key={`${x}-${y}`} cx={64 + x * 8} cy={32 + y * 8} r="1.2" />
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
            <Signal d="M30 107h73q16 0 16 16v35q0 17 17 17h70q16 0 16-16v-19" />
            <Signal d="M207 99h90q19 0 19 19v40q0 17-17 17h-41" />
            <Definition x={176} y={100} />
            <circle className="run-art-terminal" cx="30" cy="107" r="4" />
            <path className="run-art-check" d="m250 172 5 5 9-10" />
          </>
        )}

        {mode === "production" && (
          <>
            <Signal d="M115 105h46q19 0 19-19V65q0-20 20-20h44" />
            <Signal d="M115 105h129" />
            <Signal d="M115 105h46q19 0 19 19v21q0 20 20 20h44" />
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
            <Definition x={84} y={105} />
            <g className="run-art-destinations">
              <path d="m260 38-8 7 8 7m17-14 8 7-8 7m-6-17-5 20" />
              <path d="m262 95-10 10 10 10m14-20 10 10-10 10" />
              <circle cx="269" cy="165" r="13" />
              <path d="M269 157v8l6 4" />
            </g>
            <g className="run-art-online" fill="currentColor">
              <circle cx="308" cy="45" r="2.5" />
              <circle cx="308" cy="105" r="2.5" />
              <circle cx="308" cy="165" r="2.5" />
            </g>
          </>
        )}
      </svg>
    </div>
  );
}
