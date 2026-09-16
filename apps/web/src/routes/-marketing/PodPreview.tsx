import { Definition, Signal } from "./IllustrationPrimitives";
import { usePreviewActivity } from "./usePreviewActivity";

export function PodPreview() {
  const { active, previewRef } = usePreviewActivity();
  return (
    <div
      className="pod-illustration"
      ref={previewRef}
      data-animation-state={active ? "running" : "paused"}
    >
      <svg viewBox="0 0 560 230" fill="none" aria-hidden="true">
        <g className="pod-container-dots">
          {Array.from({ length: 17 }, (_, x) =>
            Array.from({ length: 13 }, (_, y) => (
              <circle
                key={`${x}-${y}`}
                cx={190 + x * 11}
                cy={44 + y * 11}
                r="1"
                opacity={Math.max(0.1, 0.65 - Math.hypot(x - 5, y - 3) / 22)}
              />
            )),
          )}
        </g>
        <path
          className="pod-container-edge"
          d="M186 49h177v134H186ZM186 49l17-17h177v134l-17 17M363 49l17-17"
        />
        <path className="pod-container-inset" d="M237 85h76v61h-76z" />
        <Definition x={275} y={115} />
        <Signal d="M63 115h119" />
        <Signal d="M382 115h43q20 0 20-20V72q0-15 15-15h38" phase={1} />
        <Signal d="M382 115h43q20 0 20 20v26q0 15 15 15h38" phase={1.4} />
        <g className="pod-port">
          <circle cx="503" cy="57" r="5" />
          <circle cx="503" cy="176" r="5" />
          <circle cx="57" cy="115" r="5" />
        </g>
        <g className="pod-diagram-label">
          <text x="57" y="94" textAnchor="middle">
            command
          </text>
          <text x="503" y="36" textAnchor="middle">
            HTTP
          </text>
          <text x="503" y="209" textAnchor="middle">
            TCP
          </text>
        </g>
      </svg>
      <code className="pod-command">python -m http.server 8000</code>
      <p className="pod-caption">A container command, with ports your app can reach.</p>
    </div>
  );
}
