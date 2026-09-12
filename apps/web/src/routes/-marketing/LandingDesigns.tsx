import { Link } from "@tanstack/react-router";

export const landingDesigns = [
  { path: "/1", key: "depth", name: "Soft depth" },
  { path: "/2", key: "horizons", name: "Cloud horizons" },
  { path: "/3", key: "studio", name: "Cloud studio" },
] as const;

export type LandingDesign = (typeof landingDesigns)[number]["key"];

export function LandingDesignNavigation({ current }: { current: LandingDesign }) {
  const selected = landingDesigns.find((design) => design.key === current);
  return (
    <nav className="landing-design-nav" aria-label="Landing page designs">
      <span>{selected?.name}</span>
      <div>
        {landingDesigns.map((design, index) => (
          <Link
            key={design.key}
            to={design.path}
            aria-label={`${index + 1}: ${design.name}`}
            aria-current={design.key === current ? "page" : undefined}
            title={design.name}
          >
            {index + 1}
          </Link>
        ))}
      </div>
      <Link to="/">Original</Link>
    </nav>
  );
}
