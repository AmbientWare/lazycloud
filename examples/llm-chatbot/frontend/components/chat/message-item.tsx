import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { CopyButton } from "./copy-button";
import { TypingIndicator } from "./typing-indicator";
import type { Message } from "@/lib/chat-storage";

interface MessageItemProps {
  message: Message;
}

export function MessageItem({ message }: MessageItemProps) {
  const isUser = message.role === "user";

  return (
    <div className="group py-3">
      <div className="max-w-3xl mx-auto px-4">
        <div className={`flex gap-4 p-4 rounded-2xl ${isUser ? "" : "bg-muted/50"}`}>
          <Avatar className="size-8 shrink-0">
            <AvatarFallback
              className={isUser ? "bg-primary text-primary-foreground" : "bg-secondary"}
            >
              {isUser ? "Y" : "A"}
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0 pt-0.5">
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-sm font-semibold">{isUser ? "You" : "Assistant"}</span>
              {!isUser && message.content && <CopyButton text={message.content} />}
            </div>
            <div className="text-[15px] leading-7 whitespace-pre-wrap break-words text-foreground/90">
              {message.content || <TypingIndicator />}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
