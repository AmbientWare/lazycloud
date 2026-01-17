"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppSidebar, EmptyState, MessageItem, ChatInput } from "@/components/chat";
import {
  type Message,
  type ChatSession,
  generateId,
  generateTitle,
  saveChats,
  loadChats,
  saveCurrentChatId,
  loadCurrentChatId,
  clearChatStorage,
} from "@/lib/chat-storage";

export default function Chat() {
  const [chats, setChats] = useState<ChatSession[]>([]);
  const [currentChatId, setCurrentChatId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const currentChat = chats.find((c) => c.id === currentChatId);
  const messages = currentChat?.messages || [];

  // Load chats from localStorage on mount
  useEffect(() => {
    const savedChats = loadChats();
    if (savedChats.length > 0) {
      setChats(savedChats);
      const savedCurrentId = loadCurrentChatId();
      if (savedCurrentId && savedChats.find((c) => c.id === savedCurrentId)) {
        setCurrentChatId(savedCurrentId);
      } else {
        const mostRecent = savedChats.sort(
          (a, b) => b.updatedAt.getTime() - a.updatedAt.getTime()
        )[0];
        setCurrentChatId(mostRecent.id);
      }
    }
    textareaRef.current?.focus();
  }, []);

  // Save chats to localStorage when they change
  useEffect(() => {
    if (chats.length > 0) {
      saveChats(chats);
    }
    if (currentChatId) {
      saveCurrentChatId(currentChatId);
    }
  }, [chats, currentChatId]);

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
        clearChatStorage();
      }
      return filtered;
    });
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

      const updateAssistantMessage = (content: string) => {
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

      updateAssistantMessage("");

      const decoder = new TextDecoder();

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        accumulatedContent += chunk;
        updateAssistantMessage(accumulatedContent);
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

        <ChatInput
          value={input}
          onChange={setInput}
          onSend={() => sendMessage()}
          isLoading={isLoading}
          textareaRef={textareaRef}
        />
      </SidebarInset>
    </SidebarProvider>
  );
}
