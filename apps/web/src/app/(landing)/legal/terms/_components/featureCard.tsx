export default function FeatureCard({
  icon,
  title,
  description,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="bg-muted/30 border-border/50 rounded-lg border p-6 shadow-sm">
      <div className="mb-4 flex items-center">
        {icon}
        <h3 className="ml-3 font-semibold">{title}</h3>
      </div>
      <p className="text-muted-foreground text-sm">{description}</p>
    </div>
  );
}
