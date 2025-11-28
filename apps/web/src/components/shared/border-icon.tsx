import { cn } from "@/lib/utils";

export default function BorderIcon({
  icon,
  className,
}: {
  icon: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "border-primary/20 bg-lazycloud/10 rounded-md border p-2",
        className,
      )}
    >
      {icon}
    </div>
  );
}
