import { cn } from "@/lib/utils";

export function MarketingExampleImage({
  className,
  eager = false,
  src,
}: {
  className?: string;
  eager?: boolean;
  src: `/use-cases/${string}.webp`;
}) {
  return (
    <img
      alt=""
      className={cn("marketing-example-image h-full w-full object-cover", className)}
      decoding="async"
      fetchPriority={eager ? "high" : "auto"}
      height={1254}
      loading={eager ? "eager" : "lazy"}
      src={src}
      width={1254}
    />
  );
}
