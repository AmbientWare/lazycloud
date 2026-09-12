import { cn } from "@/lib/utils";

export function MarketingExampleImage({
  className,
  src,
}: {
  className?: string;
  src: `/use-cases/${string}.webp`;
}) {
  return (
    <img
      alt=""
      className={cn("marketing-example-image h-full w-full object-cover", className)}
      decoding="async"
      fetchPriority="auto"
      height={1254}
      loading="lazy"
      src={src}
      width={1254}
    />
  );
}
