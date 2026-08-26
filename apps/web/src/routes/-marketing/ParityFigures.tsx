import { MarketingCard } from "./MarketingPrimitives";

type ParityImageSrc = `/parity/${string}.webp`;

function ParityFigure({ src }: { src: ParityImageSrc }) {
  return (
    <MarketingCard asChild>
      <img
        alt=""
        className="aspect-[2/1] w-full object-cover"
        decoding="async"
        height={836}
        loading="lazy"
        src={src}
        width={1254}
      />
    </MarketingCard>
  );
}

export function LocalPlate() {
  return <ParityFigure src="/parity/local.webp" />;
}

export function GpuPlate() {
  return <ParityFigure src="/parity/gpu.webp" />;
}

export function ProductionPlate() {
  return <ParityFigure src="/parity/production.webp" />;
}
