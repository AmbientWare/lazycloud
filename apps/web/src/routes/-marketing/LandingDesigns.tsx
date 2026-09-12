import { Link } from "@tanstack/react-router";

export const landingDesigns = [
  { path: "/1", key: "quiet", name: "Quiet graphite" },
  { path: "/2", key: "cloud", name: "Cloud opening" },
  { path: "/3", key: "light", name: "Light canvas" },
  { path: "/4", key: "gallery", name: "Product gallery" },
  { path: "/5", key: "chapters", name: "Day / night" },
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
