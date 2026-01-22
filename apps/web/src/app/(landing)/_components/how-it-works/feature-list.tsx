interface FeatureListProps {
  items: string[];
}

export function FeatureList({ items }: FeatureListProps) {
  return (
    <ul className="space-y-2 text-sm text-muted-foreground">
      {items.map((item) => (
        <li key={item} className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-lazycloud" />
          {item}
        </li>
      ))}
    </ul>
  );
}
