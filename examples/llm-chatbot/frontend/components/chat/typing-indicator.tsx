export function TypingIndicator() {
  return (
    <div className="flex items-center gap-1 py-1">
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:0ms]" />
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:150ms]" />
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:300ms]" />
    </div>
  );
}
