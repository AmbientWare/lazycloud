import { Cloud, Cpu, Server } from "lucide-react";
import { Definition, Signal } from "./IllustrationPrimitives";
import { usePreviewActivity } from "./usePreviewActivity";
import "./computePlacement.css";

export const computeDestinations = [
  {
    key: "managed",
    title: "LazyCloud",
    capacity: "Managed CPU",
    body: "Deploy without managing servers. LazyCloud provisions CPU capacity and scales down when idle.",
    Icon: Cloud,
    path: "M112 170h30q24 0 24-24V80q0-24 24-24h16",
  },
  {
    key: "aws",
    title: "Your AWS account",
    capacity: "CPU and GPU",
    body: "Use CPU and GPU capacity in your AWS account. Manage deployments through LazyCloud.",
    Icon: Cpu,
    path: "M112 170h94",
  },
  {
    key: "machines",
    title: "Your own machines",
    capacity: "Linux servers and GPUs",
    body: "Connect your Linux servers, VMs, or GPU machines. Deploy through the same Python API.",
    Icon: Server,
    path: "M112 170h30q24 0 24 24v66q0 24 24 24h16",
  },
] as const;

export type ComputeDestination = (typeof computeDestinations)[number]["key"];

export function ComputePlacement({ selected }: { selected: ComputeDestination }) {
  const { active, previewRef } = usePreviewActivity();
  return (
    <div
      className="compute-map"
      ref={previewRef}
      data-animation-state={active ? "running" : "paused"}
    >
      <div className="compute-map-diagram" aria-hidden="true">
        <svg
          className="compute-map-paths"
          viewBox="0 0 400 340"
          preserveAspectRatio="none"
          fill="none"
        >
          {computeDestinations.map((destination) => (
            <g
              className="compute-map-route"
              data-selected={selected === destination.key}
              key={destination.key}
            >
              <Signal d={destination.path} />
            </g>
          ))}
        </svg>
        <div className="compute-map-source">
          <svg viewBox="0 0 120 120" fill="none">
            <g className="compute-map-dots">
              {Array.from({ length: 13 }, (_, x) =>
                Array.from({ length: 13 }, (_, y) => {
                  const distance = Math.hypot(x - 6, y - 6);
                  return distance > 4 && distance < 6.5 ? (
                    <circle
                      key={`${x}-${y}`}
                      cx={12 + x * 8}
                      cy={12 + y * 8}
                      r="1"
                      opacity={1 - distance / 8}
                    />
                  ) : null;
                }),
              )}
            </g>
            <Definition x={60} y={60} />
          </svg>
          <span>Your application</span>
        </div>
        <div className="compute-map-destinations">
          {computeDestinations.map(({ key, title, capacity, Icon }) => (
            <div className="compute-map-destination" data-selected={selected === key} key={key}>
              <span className="compute-map-icon">
                <Icon strokeWidth={1.25} />
              </span>
              <div>
                <strong>{title}</strong>
                <span>{capacity}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
      <p className="compute-map-caption">One Python API. Your choice of infrastructure.</p>
    </div>
  );
}
