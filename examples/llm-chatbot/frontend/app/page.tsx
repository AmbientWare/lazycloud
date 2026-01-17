"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarMenuAction,
  SidebarProvider,
  SidebarInset,
  SidebarTrigger,
  SidebarGroup,
  SidebarGroupContent,
} from "@/components/ui/sidebar";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Send,
  Loader2,
  Bot,
  Copy,
  Check,
  MessageCircle,
  Code,
  Lightbulb,
  HelpCircle,
  Plus,
  Trash2,
  MessageSquare,
} from "lucide-react";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

interface ChatSession {
  id: string;
  title: string;
  messages: Message[];
  createdAt: Date;
  updatedAt: Date;
}

const CHATS_KEY = "lazycloud_chats";
const CURRENT_CHAT_KEY = "lazycloud_current_chat";

const generateId = () => Math.random().toString(36).substring(2, 9);

const generateTitle = (messages: Message[]): string => {
  const firstUserMessage = messages.find((m) => m.role === "user");
  if (firstUserMessage) {
    const content = firstUserMessage.content;
    return content.length > 40 ? content.substring(0, 40) + "..." : content;
  }
  return "New Chat";
};

const saveChats = (chats: ChatSession[]) => {
  if (typeof window === "undefined") return;
  localStorage.setItem(
    CHATS_KEY,
    JSON.stringify(
      chats.map((chat) => ({
        ...chat,
        messages: chat.messages.map((m) => ({
          ...m,
          timestamp: m.timestamp.toISOString(),
        })),
        createdAt: chat.createdAt.toISOString(),
        updatedAt: chat.updatedAt.toISOString(),
      }))
    )
  );
};

const loadChats = (): ChatSession[] => {
  if (typeof window === "undefined") return [];
  try {
    const stored = localStorage.getItem(CHATS_KEY);
    if (!stored) return [];
    return JSON.parse(stored).map(
      (chat: {
        id: string;
        title: string;
        messages: { id: string; role: "user" | "assistant"; content: string; timestamp: string }[];
        createdAt: string;
        updatedAt: string;
      }) => ({
        ...chat,
        messages: chat.messages.map((m) => ({
          ...m,
          timestamp: new Date(m.timestamp),
        })),
        createdAt: new Date(chat.createdAt),
        updatedAt: new Date(chat.updatedAt),
      })
    );
  } catch {
    return [];
  }
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          onClick={handleCopy}
          className="size-7 opacity-0 group-hover:opacity-100 transition-opacity"
        >
          {copied ? (
            <Check className="size-3.5 text-emerald-500" />
          ) : (
            <Copy className="size-3.5" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        <p>{copied ? "Copied!" : "Copy message"}</p>
      </TooltipContent>
    </Tooltip>
  );
}

function TypingIndicator() {
  return (
    <div className="flex items-center gap-1 py-1">
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:0ms]" />
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:150ms]" />
      <span className="size-2 bg-primary/60 rounded-full animate-bounce [animation-delay:300ms]" />
    </div>
  );
}

function EmptyState({ onSuggestionClick }: { onSuggestionClick: (text: string) => void }) {
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

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] max-w-xl mx-auto px-4 py-12">
      <Avatar className="size-14 mb-6">
        <AvatarFallback className="bg-primary text-primary-foreground">
          <Bot className="size-7" />
        </AvatarFallback>
      </Avatar>
      <h1 className="text-2xl font-bold tracking-tight mb-1">LLM Chat</h1>
      <p className="text-muted-foreground text-center mb-8">
        Ask anything
      </p>
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

function MessageItem({ message }: { message: Message }) {
  const isUser = message.role === "user";

  return (
    <div className="group py-3">
      <div className="max-w-3xl mx-auto px-4">
        <div className={`flex gap-4 p-4 rounded-2xl ${isUser ? "" : "bg-muted/50"}`}>
          <Avatar className="size-8 shrink-0">
            <AvatarFallback className={isUser ? "bg-primary text-primary-foreground" : "bg-secondary"}>
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

function AppSidebar({
  chats,
  currentChatId,
  onSelectChat,
  onDeleteChat,
  onNewChat,
}: {
  chats: ChatSession[];
  currentChatId: string | null;
  onSelectChat: (id: string) => void;
  onDeleteChat: (id: string) => void;
  onNewChat: () => void;
}) {
  const sortedChats = [...chats].sort((a, b) => b.updatedAt.getTime() - a.updatedAt.getTime());

  return (
    <Sidebar>
      <SidebarHeader className="p-3">
        <Button variant="outline" className="w-full justify-start gap-2" onClick={onNewChat}>
          <Plus className="size-4" />
          New chat
        </Button>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {sortedChats.length === 0 ? (
                <p className="text-sm text-muted-foreground text-center py-4">No chats yet</p>
              ) : (
                sortedChats.map((chat) => (
                  <SidebarMenuItem key={chat.id}>
                    <SidebarMenuButton
                      isActive={chat.id === currentChatId}
                      onClick={() => onSelectChat(chat.id)}
                    >
                      <MessageSquare className="size-4" />
                      <span className="truncate">{chat.title}</span>
                    </SidebarMenuButton>
                    <AlertDialog>
                      <AlertDialogTrigger asChild>
                        <SidebarMenuAction showOnHover>
                          <Trash2 className="size-4" />
                        </SidebarMenuAction>
                      </AlertDialogTrigger>
                      <AlertDialogContent>
                        <AlertDialogHeader>
                          <AlertDialogTitle>Delete chat?</AlertDialogTitle>
                          <AlertDialogDescription>
                            This will permanently delete this chat and all its messages.
                          </AlertDialogDescription>
                        </AlertDialogHeader>
                        <AlertDialogFooter>
                          <AlertDialogCancel>Cancel</AlertDialogCancel>
                          <AlertDialogAction onClick={() => onDeleteChat(chat.id)}>
                            Delete
                          </AlertDialogAction>
                        </AlertDialogFooter>
                      </AlertDialogContent>
                    </AlertDialog>
                  </SidebarMenuItem>
                ))
              )}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}

export default function Chat() {
  const [chats, setChats] = useState<ChatSession[]>([]);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const currentChat = chats.find((c) => c.id === currentChatId);
  const messages = currentChat?.messages || [];

  useEffect(() => {
    const savedChats = loadChats();
    if (savedChats.length > 0) {
      setChats(savedChats);
      const savedCurrentId = localStorage.getItem(CURRENT_CHAT_KEY);
      if (savedCurrentId && savedChats.find((c) => c.id === savedCurrentId)) {
        setCurrentChatId(savedCurrentId);
      } else {
        const mostRecent = savedChats.sort(
          (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
        )[0];
        setCurrentChatId(mostRecent.id);
      }
    }
    // Auto-focus input on page load
    textareaRef.current?.focus();
  }, []);

  useEffect(() => {
    if (chats.length > 0) {
      saveChats(chats);
    }
    if (currentChatId) {
      localStorage.setItem(CURRENT_CHAT_KEY, currentChatId);
    }
  }, [chats, currentChatId]);

  // Auto-resize textarea based on content
  useEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.min(textarea.scrollHeight, 150)}px`;
    }
  }, [input]);

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const createNewChat = () => {
    const newChat: ChatSession = {
      id: generateId(),
      title: "New Chat",
      messages: [],
      createdAt: new Date(),
      updatedAt: new Date(),
    };
    setChats((prev) => [newChat, ...prev]);
    setCurrentChatId(newChat.id);
    // Auto-focus input on new chat
    setTimeout(() => textareaRef.current?.focus(), 0);
  };

  const selectChat = (chatId: string) => {
    setCurrentChatId(chatId);
  };

  const deleteChat = (chatId: string) => {
    setChats((prev) => {
      const filtered = prev.filter((c) => c.id !== chatId);
      if (chatId === currentChatId) {
        if (filtered.length > 0) {
          setCurrentChatId(filtered[0].id);
        } else {
          setCurrentChatId(null);
        }
      }
      if (filtered.length === 0) {
        localStorage.removeItem(CHATS_KEY);
        localStorage.removeItem(CURRENT_CHAT_KEY);
      }
      return filtered;
    });
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const sendMessage = async (overrideInput?: string) => {
    const messageContent = overrideInput || input;
    if (!messageContent.trim() || isLoading) return;

    const userMessage: Message = {
      id: generateId(),
      role: "user",
      content: messageContent.trim(),
      timestamp: new Date(),
    };

    let chatId = currentChatId;
    let newMessages: Message[];

    if (!chatId) {
      const newChat: ChatSession = {
        id: generateId(),
        title: generateTitle([userMessage]),
        messages: [userMessage],
        createdAt: new Date(),
        updatedAt: new Date(),
      };
      setChats((prev) => [newChat, ...prev]);
      setCurrentChatId(newChat.id);
      chatId = newChat.id;
      newMessages = [userMessage];
    } else {
      newMessages = [...messages, userMessage];
      setChats((prev) =>
        prev.map((c) =>
          c.id === chatId
            ? {
                ...c,
                messages: newMessages,
                title: c.messages.length === 0 ? generateTitle(newMessages) : c.title,
                updatedAt: new Date(),
              }
            : c
        )
      );
    }

    setInput("");
    setIsLoading(true);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 5 * 60 * 1000);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

      const response = await fetch(`${apiUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: chatId,
          messages: newMessages.map((m) => ({ role: m.role, content: m.content })),
          stream: true,
        }),
        signal: controller.signal,
      });

      if (!response.ok) {
        throw new Error(`Failed to get response: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No reader available");

      const assistantMessageId = generateId();
      let accumulatedContent = "";

      const addAssistantMessage = (content: string) => {
        setChats((prev) =>
          prev.map((c) =>
            c.id === chatId
              ? {
                  ...c,
                  messages: [
                    ...newMessages,
                    {
                      id: assistantMessageId,
                      role: "assistant" as const,
                      content,
                      timestamp: new Date(),
                    },
                  ],
                  updatedAt: new Date(),
                }
              : c
          )
        );
      };

      addAssistantMessage("");

      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        accumulatedContent += chunk;
        addAssistantMessage(accumulatedContent);
      }
    } catch (error) {
      console.error("Error:", error);
      setChats((prev) =>
        prev.map((c) =>
          c.id === chatId
            ? {
                ...c,
                messages: [
                  ...newMessages,
                  {
                    id: generateId(),
                    role: "assistant" as const,
                    content: "Sorry, I encountered an error. Please try again.",
                    timestamp: new Date(),
                  },
                ],
                updatedAt: new Date(),
              }
            : c
        )
      );
    } finally {
      clearTimeout(timeoutId);
      setIsLoading(false);
      textareaRef.current?.focus();
    }
  };

  return (
    <SidebarProvider>
      <AppSidebar
        chats={chats}
        currentChatId={currentChatId}
        onSelectChat={selectChat}
        onDeleteChat={deleteChat}
        onNewChat={createNewChat}
      />
      <SidebarInset className="flex flex-col h-screen">
        <div className="flex-1 overflow-y-auto">
          {messages.length === 0 ? (
            <EmptyState onSuggestionClick={(text) => sendMessage(text)} />
          ) : (
            <div className="pt-4 pb-4">
              {messages.map((message) => (
                <MessageItem key={message.id} message={message} />
              ))}
              <div ref={messagesEndRef} />
            </div>
          )}
        </div>

        <div className="shrink-0 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="max-w-3xl mx-auto p-4">
            <Card className="p-2 py-2">
              <CardContent className="flex items-end gap-2 p-0">
                <SidebarTrigger className="md:hidden size-9 shrink-0" />
                <Textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Send a message..."
                  className="min-h-[40px] max-h-[150px] resize-none border-0 shadow-none focus-visible:ring-0 overflow-y-auto"
                  rows={1}
                  disabled={isLoading}
                />
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      onClick={() => sendMessage()}
                      disabled={isLoading || !input.trim()}
                      size="icon"
                      className="size-9 shrink-0"
                    >
                      {isLoading ? (
                        <Loader2 className="size-4 animate-spin" />
                      ) : (
                        <Send className="size-4" />
                      )}
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>
                    <p>Send message</p>
                  </TooltipContent>
                </Tooltip>
              </CardContent>
            </Card>
            <p className="text-[11px] text-muted-foreground/70 text-center mt-2">
              Press Enter to send, Shift+Enter for new line
            </p>
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
