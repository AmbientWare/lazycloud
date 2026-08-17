type ParityImageSrc = `/parity/${string}.webp`;

function ParityFigure({ src }: { src: ParityImageSrc }) {
  return (
    <img
      alt=""
      className="aspect-[2/1] w-full rounded-xl border border-border bg-card object-cover"
      decoding="async"
      height={836}
      loading="lazy"
      src={src}
      width={1254}
    />
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
