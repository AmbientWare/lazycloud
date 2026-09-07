import { useId, type ComponentProps, type ReactNode } from "react";

import { Input } from "@/components/ui/input";

export function FormField({
  label,
  hint,
  error,
  id: suppliedId,
  ...props
}: ComponentProps<typeof Input> & {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
}) {
  const generatedId = useId();
  const id = suppliedId ?? generatedId;
  const description = [
    props["aria-describedby"],
    hint ? `${id}-hint` : null,
    error ? `${id}-error` : null,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="min-w-0 space-y-1.5">
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <Input
        {...props}
        id={id}
        aria-invalid={error ? true : props["aria-invalid"]}
        aria-describedby={description || undefined}
      />
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-muted-foreground">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}
