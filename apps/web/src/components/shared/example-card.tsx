import Link from "next/link";
import { ArrowRight } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface ExampleCardProps {
  title: string;
  description: string;
  href: string;
  stack: string[];
}

export function ExampleCard({
  title,
  description,
  href,
  stack,
}: ExampleCardProps) {
  return (
    <Link
      href={href}
      className={cn(
        "group flex flex-col gap-4 rounded-xl border p-6",
        "bg-card hover:bg-accent/50 transition-colors",
        "hover:border-lazycloud/50"
      )}
    >
      <div className="flex-1">
        <h3 className="font-semibold text-lg group-hover:text-lazycloud transition-colors">
          {title}
        </h3>
        <p className="text-muted-foreground text-sm mt-2 leading-relaxed">
          {description}
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {stack.map((tech) => (
          <Badge key={tech} variant="secondary" className="text-xs">
            {tech}
          </Badge>
        ))}
      </div>
      <div className="flex items-center text-sm text-muted-foreground group-hover:text-lazycloud transition-colors">
        View example
        <ArrowRight className="ml-1 h-4 w-4 group-hover:translate-x-1 transition-transform" />
      </div>
    </Link>
  );
}
