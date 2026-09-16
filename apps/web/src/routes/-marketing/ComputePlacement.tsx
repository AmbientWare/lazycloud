import { useId } from "react";
import { usePreviewActivity } from "./usePreviewActivity";
import "./computePlacement.css";

export const computeDestinations = [
  {
    key: "managed",
    title: "LazyCloud",
    body: "Deploy without managing servers. LazyCloud provisions CPU capacity and scales down when idle.",
  },
  {
    key: "aws",
    title: "Your AWS account",
    body: "Use CPU and GPU capacity in your AWS account. Manage deployments through LazyCloud.",
  },
  {
    key: "machines",
    title: "Your own machines",
    body: "Connect your Linux servers, VMs, or GPU machines. Deploy through the same Python API.",
  },
] as const;

export type ComputeDestination = (typeof computeDestinations)[number]["key"];

const cells = Array.from({ length: 64 }, (_, index) => ({
  x: index % 8,
  y: Math.floor(index / 8),
}));

function ComputePlane({
  selected,
  surfaceId,
  cellId,
}: {
  selected: boolean;
  surfaceId: string;
  cellId: string;
}) {
  return (
    <g className="compute-plane" data-selected={selected}>
      <path className="compute-plane-shadow" d="M0 14 224 114 0 214-224 114Z" />
      <path className="compute-plane-left" d="M-224 100 0 200v10l-224-100Z" />
      <path className="compute-plane-right" d="m0 200 224-100v10L0 210Z" />
      <path
        className="compute-plane-top"
        d="M0 0 224 100 0 200-224 100Z"
        fill={`url(#${surfaceId})`}
      />
      <g transform="matrix(1.12 .5 -1.12 .5 0 0)">
        <path className="compute-plane-rim" d="M5 5H195V195H5Z" />
        {cells.map(({ x, y }) => (
          <rect
            className="compute-cell"
            data-workload={x >= 2 && x <= 5 && y >= 2 && y <= 5}
            key={`${x}-${y}`}
            x={9 + x * 23}
            y={9 + y * 23}
            width="21"
            height="21"
            fill={`url(#${cellId})`}
          />
        ))}
        <g className="compute-workload">
          <rect className="compute-workload-shadow" x="56" y="56" width="88" height="88" />
          <rect className="compute-workload-face" x="54" y="54" width="88" height="88" />
          <path
            className="compute-workload-mark"
            d="M84 81h-8v12l-5 5 5 5v12h8m28-34h8v12l5 5-5 5v12h-8"
          />
          <path className="compute-workload-core" d="m98 90 8 8-8 8-8-8Z" />
        </g>
      </g>
      <path className="compute-plane-edge" d="m-224 100 224 100 224-100" />
      <path className="compute-plane-signal" pathLength="100" d="m-224 100 224 100 224-100" />
      {Array.from({ length: 16 }, (_, index) => (
        <path
          key={index}
          className="compute-plane-vent"
          d={`m${14 + index * 12} ${202 - index * 5.36}v3`}
        />
      ))}
    </g>
  );
}

export function ComputePlacement({ selected }: { selected: ComputeDestination }) {
  const { active, previewRef } = usePreviewActivity();
  const id = useId();
  return (
    <div
      className="compute-sculpture"
      ref={previewRef}
      data-animation-state={active ? "running" : "paused"}
      data-destination={selected}
      aria-hidden="true"
    >
      <svg viewBox="0 0 600 540" fill="none">
        <defs>
          <linearGradient
            id={`${id}-surface`}
            x1="-140"
            y1="0"
            x2="100"
            y2="220"
            gradientUnits="userSpaceOnUse"
          >
            <stop stopColor="#33383d" />
            <stop offset="0.48" stopColor="#1c2024" />
            <stop offset="1" stopColor="#101214" />
          </linearGradient>
          <linearGradient id={`${id}-cell`} x1="0" y1="0" x2="1" y2="1">
            <stop stopColor="#606970" stopOpacity=".28" />
            <stop offset="1" stopColor="#252b30" stopOpacity=".12" />
          </linearGradient>
          <radialGradient id={`${id}-floor`}>
            <stop stopColor="var(--brand)" stopOpacity=".1" />
            <stop offset="1" stopColor="var(--brand)" stopOpacity="0" />
          </radialGradient>
          <pattern id={`${id}-grid`} width="24" height="24" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r=".75" fill="currentColor" />
          </pattern>
          <radialGradient id={`${id}-fade`}>
            <stop stopColor="white" />
            <stop offset="1" stopColor="black" />
          </radialGradient>
          <mask id={`${id}-mask`}>
            <rect width="600" height="540" fill={`url(#${id}-fade)`} />
          </mask>
        </defs>
        <ellipse cx="300" cy="416" rx="284" ry="122" fill={`url(#${id}-floor)`} />
        <g mask={`url(#${id}-mask)`} className="compute-ground">
          <path d="m20 370 280-125 280 125-280 125Z" />
          <path d="m-40 370 340-152 340 152-340 152Z" />
          <g transform="matrix(1.12 .5 -1.12 .5 300 40)">
            <rect width="480" height="480" fill={`url(#${id}-grid)`} stroke="none" />
          </g>
        </g>
        {[...computeDestinations].reverse().map(({ key }, index) => (
          <g key={key} data-layer={key} transform={`translate(300 ${70 + (2 - index) * 112})`}>
            <ComputePlane
              selected={selected === key}
              surfaceId={`${id}-surface`}
              cellId={`${id}-cell`}
            />
          </g>
        ))}
      </svg>
    </div>
  );
}
