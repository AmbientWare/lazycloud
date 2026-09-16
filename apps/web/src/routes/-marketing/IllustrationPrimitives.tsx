import "./runModeArt.css";

export function Definition({ x, y, small = false }: { x: number; y: number; small?: boolean }) {
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

export function Signal({ d, phase = 0 }: { d: string; phase?: number }) {
  const style = { animationDelay: `calc(var(--run-delay) + ${phase}s)` };
  return (
    <>
      <path className="run-art-route" d={d} />
      <path className="run-art-signal" d={d} pathLength="100" style={style} />
      <path className="run-art-signal run-art-signal-tip" d={d} pathLength="100" style={style} />
    </>
  );
}
