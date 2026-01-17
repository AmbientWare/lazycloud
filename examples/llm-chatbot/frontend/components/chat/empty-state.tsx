import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Card, CardContent } from "@/components/ui/card";
import { Bot, Code, Lightbulb, HelpCircle, MessageCircle } from "lucide-react";

interface EmptyStateProps {
  onSuggestionClick: (text: string) => void;
}

const suggestions = [
  {
    icon: HelpCircle,
    text: "Explain how Docker containers work",
    color: "text-blue-500",
  },
  {
    icon: Code,
    text: "Write a Python API endpoint",
    color: "text-emerald-500",
  },
  {
    icon: Lightbulb,
    text: "Help me brainstorm project ideas",
    color: "text-amber-500",
  },
  {
    icon: MessageCircle,
    text: "Debug this error message for me",
    color: "text-rose-500",
  },
];

export function EmptyState({ onSuggestionClick }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] max-w-xl mx-auto px-4 py-12">
      <Avatar className="size-14 mb-6">
        <AvatarFallback className="bg-primary text-primary-foreground">
          <Bot className="size-7" />
        </AvatarFallback>
      </Avatar>
      <h1 className="text-2xl font-bold tracking-tight mb-1">LLM Chat</h1>
      <p className="text-muted-foreground text-center mb-8">Ask anything</p>
      <div className="grid gap-3 w-full">
        {suggestions.map((suggestion, i) => (
          <Card
            key={i}
            className="cursor-pointer hover:bg-accent/50 hover:border-primary/20 transition-all py-0"
            onClick={() => onSuggestionClick(suggestion.text)}
          >
            <CardContent className="flex items-center gap-3 p-4">
              <div className={`p-2 rounded-lg bg-secondary ${suggestion.color}`}>
                <suggestion.icon className="size-4" />
              </div>
              <span className="text-sm">{suggestion.text}</span>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}
