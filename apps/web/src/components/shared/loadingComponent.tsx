import { Spinner } from "./spinner";

export default function Loading() {
  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="flex flex-col items-center gap-4">
        <Spinner size="lg" className="h-10 w-10" />
        <p className="text-muted-foreground animate-pulse text-sm">Loading...</p>
      </div>
    </div>
  );
}
