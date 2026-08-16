/**
 * The three places one function runs, drawn as isometric plates.
 *
 * The house illustration language is a drafting plate: an isometric object on
 * paper, graphite construction lines, registration marks at the corners, and one
 * accent doing the pointing. The example cards use rendered art in that style;
 * these are drawn as SVG instead so they inherit `--brand` and `--foreground`
 * and stay crisp at any size — the same drawing, not a picture of one.
 *
 * The same cube appears in all three, because that is the whole argument: one
 * function, moved. Only what surrounds it changes — nothing, a card, or copies
 * of itself.
 */

/* Isometric projection: a unit of depth travels 0.866 across and 0.5 down. */
const RUN = 0.866;

type Point = readonly [number, number];

function polygon(points: readonly Point[]): string {
  return points.map(([x, y]) => `${x},${y}`).join(" ");
}

/**
 * One cube standing on the ground plane, its near-bottom corner at (x, y).
 *
 * `ghost` draws capacity that is not running: the same volume, dashed and
 * unfilled, so a reader counts it as the same thing rather than a different one.
 */
function Cube({ x, y, size, accent = false, ghost = false }: {
  x: number;
  y: number;
  size: number;
  accent?: boolean;
  ghost?: boolean;
}) {
  const run = RUN * size;
  const rise = size / 2;
  const top: Point[] = [
    [x, y - size],
    [x + run, y - size + rise],
    [x, y],
    [x - run, y - size + rise],
  ];
  const left: Point[] = [
    [x - run, y - size + rise],
    [x, y],
    [x, y + size],
    [x - run, y + rise],
  ];
  const right: Point[] = [
    [x + run, y - size + rise],
    [x, y],
    [x, y + size],
    [x + run, y + rise],
  ];
  if (ghost) {
    return (
      <g fill="none" stroke="var(--foreground)" strokeOpacity={0.28} strokeDasharray="3 3">
        <polygon points={polygon(top)} />
        <polygon points={polygon(left)} />
        <polygon points={polygon(right)} />
      </g>
    );
  }
  return (
    <g stroke="var(--foreground)" strokeOpacity={0.55} strokeLinejoin="round">
      <polygon points={polygon(top)} fill="var(--brand)" fillOpacity={accent ? 0.32 : 0.16} />
      <polygon points={polygon(left)} fill="var(--foreground)" fillOpacity={0.05} />
      <polygon points={polygon(right)} fill="var(--foreground)" fillOpacity={0.11} />
    </g>
  );
}

/** A card sliding into the machine: a thin slab, on end, slotted. */
function AcceleratorCard({ x, y, size }: { x: number; y: number; size: number }) {
  const run = RUN * size;
  const thickness = size * 0.16;
  const height = size * 0.95;
  const face: Point[] = [
    [x, y - height],
    [x + run, y - height + size / 2],
    [x + run, y + size / 2],
    [x, y],
  ];
  const edge: Point[] = [
    [x, y - height],
    [x - thickness * RUN, y - height + thickness / 2],
    [x - thickness * RUN, y + thickness / 2],
    [x, y],
  ];
  return (
    <g stroke="var(--foreground)" strokeOpacity={0.55} strokeLinejoin="round">
      <polygon points={polygon(face)} fill="var(--brand)" fillOpacity={0.42} />
      <polygon points={polygon(edge)} fill="var(--foreground)" fillOpacity={0.13} />
      {/* Slots, the detail that makes it read as a card rather than a wall. */}
      {[0.3, 0.5, 0.7].map((t) => (
        <line
          key={t}
          x1={x + run * 0.2}
          y1={y - height * t + size * 0.1}
          x2={x + run * 0.85}
          y2={y - height * t + size * 0.42}
          strokeOpacity={0.45}
        />
      ))}
    </g>
  );
}

/** Registration marks, the drafting tell the rendered plates carry. */
function Marks() {
  const at: Point[] = [
    [14, 14],
    [186, 14],
    [14, 126],
    [186, 126],
  ];
  return (
    <g stroke="var(--foreground)" strokeOpacity={0.22}>
      {at.map(([x, y]) => (
        <g key={`${x}-${y}`}>
          <line x1={x - 5} y1={y} x2={x + 5} y2={y} />
          <line x1={x} y1={y - 5} x2={x} y2={y + 5} />
        </g>
      ))}
    </g>
  );
}

/** The ground the objects sit on, so they read as placed rather than floating.
 *
 * Takes both ends rather than one height: a row that recedes sits on a plane
 * that recedes with it, and a level line under a climbing row reads as a mistake.
 */
function GroundLine({ from, to }: { from: Point; to: Point }) {
  return (
    <line
      x1={from[0]}
      y1={from[1]}
      x2={to[0]}
      y2={to[1]}
      stroke="var(--foreground)"
      strokeOpacity={0.16}
      strokeDasharray="2 4"
    />
  );
}

function Plate({ children }: { children: React.ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      className="h-[184px] w-full rounded-xl border border-border bg-card"
      viewBox="0 0 200 140"
      preserveAspectRatio="xMidYMid meet"
    >
      <Marks />
      {children}
    </svg>
  );
}

/** One machine, running it in place. */
export function LocalPlate() {
  return (
    <Plate>
      <GroundLine from={[58, 96]} to={[142, 96]} />
      <Cube x={100} y={78} size={26} accent />
    </Plate>
  );
}

/** The same machine, with a card in it. */
export function GpuPlate() {
  return (
    <Plate>
      <GroundLine from={[46, 94]} to={[154, 94]} />
      <Cube x={92} y={76} size={26} />
      {/* Overlapping the right face deliberately: seated, not standing next to it. */}
      <AcceleratorCard x={104} y={72} size={26} />
    </Plate>
  );
}

/** Copies of it, and room for more. */
export function ProductionPlate() {
  const size = 17;
  const step = 30;
  const row = [0, 1, 2, 3].map((i) => ({ x: 48 + i * step, y: 104 - i * (step / 2) }));
  return (
    <Plate>
      <GroundLine from={[24, 118]} to={[176, 42]} />
      {row.map((at, i) => (
        <Cube key={at.x} x={at.x} y={at.y} size={size} accent={i < 2} ghost={i === 3} />
      ))}
    </Plate>
  );
}
