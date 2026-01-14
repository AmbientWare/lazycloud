"use client";

import { useState, useRef, useEffect, useCallback } from "react";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

interface Toast {
  id: string;
  message: string;
  type: "success" | "error";
}

// Generate unique ID
const generateId = () => Math.random().toString(36).substring(2, 9);

// Format timestamp for display
const formatTime = (date: Date) => {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

// Copy button component
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Fallback for older browsers
      const textArea = document.createElement("textarea");
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  return (
    <button
      onClick={handleCopy}
      className="copy-button absolute top-2 right-2 p-1.5 rounded-lg bg-white/80 hover:bg-white
                 text-slate-500 hover:text-slate-700 transition-all shadow-sm"
      aria-label={copied ? "Copied to clipboard" : "Copy message"}
    >
      {copied ? (
        <svg className="w-4 h-4 text-green-600 copy-success" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
        </svg>
      )}
    </button>
  );
}

// Typing indicator component
function TypingIndicator() {
  return (
    <div
      className="flex items-center gap-1 px-4 py-3"
      role="status"
      aria-label="Assistant is typing"
    >
      <span className="typing-dot w-2 h-2 bg-slate-400 rounded-full" />
      <span className="typing-dot w-2 h-2 bg-slate-400 rounded-full" />
      <span className="typing-dot w-2 h-2 bg-slate-400 rounded-full" />
    </div>
  );
}

// Toast notification component
function Toast({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  useEffect(() => {
    const timer = setTimeout(onDismiss, 5000);
    return () => clearTimeout(timer);
  }, [onDismiss]);

  return (
    <div
      className={`toast-enter flex items-center gap-3 px-4 py-3 rounded-xl shadow-lg ${
        toast.type === "error"
          ? "bg-red-50 border border-red-200 text-red-800"
          : "bg-green-50 border border-green-200 text-green-800"
      }`}
      role="alert"
      aria-live="assertive"
    >
      {toast.type === "error" ? (
        <svg className="w-5 h-5 text-red-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
        </svg>
      ) : (
        <svg className="w-5 h-5 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      )}
      <span className="text-sm font-medium flex-1">{toast.message}</span>
      <button
        onClick={onDismiss}
        className="p-1 rounded-lg hover:bg-black/5 transition-colors"
        aria-label="Dismiss notification"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );
}

// Empty state component
function EmptyState() {
  return (
    <div className="empty-state text-center py-16 px-4 rounded-2xl">
      <div className="welcome-icon w-16 h-16 mx-auto mb-6 bg-blue-100 rounded-2xl flex items-center justify-center">
        <svg className="w-8 h-8 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
        </svg>
      </div>
      <h2 className="text-2xl font-semibold text-slate-800 mb-2">Welcome to LazyCloud Chat</h2>
      <p className="text-slate-500 max-w-md mx-auto mb-6">
        Start a conversation with the AI assistant. Ask questions, get help with tasks, or just have a friendly chat.
      </p>
      <div className="flex flex-wrap justify-center gap-2">
        <span className="px-3 py-1.5 bg-white rounded-full text-sm text-slate-600 border border-slate-200">
          Ask a question
        </span>
        <span className="px-3 py-1.5 bg-white rounded-full text-sm text-slate-600 border border-slate-200">
          Get help with code
        </span>
        <span className="px-3 py-1.5 bg-white rounded-full text-sm text-slate-600 border border-slate-200">
          Brainstorm ideas
        </span>
      </div>
    </div>
  );
}

// Message component
function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";

  return (
    <article
      className={`flex ${isUser ? "justify-end" : "justify-start"} message-animation`}
      aria-label={`Message from ${message.role} at ${formatTime(message.timestamp)}`}
    >
      <div className="flex flex-col gap-1 max-w-[85%]">
        <div
          className={`message-bubble relative px-4 py-3 ${
            isUser ? "message-bubble-user" : "message-bubble-assistant"
          }`}
        >
          <p className="whitespace-pre-wrap text-[15px] leading-relaxed">{message.content}</p>
          {!isUser && message.content && <CopyButton text={message.content} />}
        </div>
        <span
          className={`text-xs text-slate-400 ${isUser ? "text-right mr-1" : "ml-1"}`}
          aria-hidden="true"
        >
          {formatTime(message.timestamp)}
        </span>
      </div>
    </article>
  );
}

// Main chat component
export default function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [charCount, setCharCount] = useState(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const MAX_CHARS = 4000;

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages, scrollToBottom]);

  const addToast = (message: string, type: "success" | "error") => {
    const toast: Toast = { id: generateId(), message, type };
    setToasts((prev) => [...prev, toast]);
  };

  const removeToast = (id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    if (value.length <= MAX_CHARS) {
      setInput(value);
      setCharCount(value.length);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(e);
    }
    if (e.key === "Escape") {
      setInput("");
      setCharCount(0);
    }
  };

  const sendMessage = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMessage: Message = {
      id: generateId(),
      role: "user",
      content: input.trim(),
      timestamp: new Date(),
    };

    const newMessages = [...messages, userMessage];
    setMessages(newMessages);
    setInput("");
    setCharCount(0);
    setIsLoading(true);

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
      const response = await fetch(`${apiUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: newMessages.map((m) => ({ role: m.role, content: m.content })),
          stream: true,
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to get response: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No reader available");

      const assistantMessage: Message = {
        id: generateId(),
        role: "assistant",
        content: "",
        timestamp: new Date(),
      };
      setMessages([...newMessages, assistantMessage]);

      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        assistantMessage.content += chunk;
        assistantMessage.timestamp = new Date();
        setMessages([...newMessages, { ...assistantMessage }]);
      }
    } catch (error) {
      console.error("Error:", error);
      addToast(
        "Something went wrong. Please check your connection and try again.",
        "error"
      );
      setMessages([
        ...newMessages,
        {
          id: generateId(),
          role: "assistant",
          content: "Sorry, I encountered an error. Please try again.",
          timestamp: new Date(),
        },
      ]);
    } finally {
      setIsLoading(false);
      inputRef.current?.focus();
    }
  };

  const clearChat = () => {
    setMessages([]);
    addToast("Conversation cleared", "success");
    inputRef.current?.focus();
  };

  return (
    <main id="main-content" className="min-h-screen flex flex-col bg-[#FAFAFA]">
      {/* Toast Container */}
      <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((toast) => (
          <Toast key={toast.id} toast={toast} onDismiss={() => removeToast(toast.id)} />
        ))}
      </div>

      {/* Header */}
      <header className="bg-white border-b border-slate-200 sticky top-0 z-40">
        <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 bg-gradient-to-br from-blue-500 to-blue-600 rounded-xl flex items-center justify-center shadow-sm">
              <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <div>
              <h1 className="text-lg font-semibold text-slate-800">
                LazyCloud <span className="text-blue-600">Chat</span>
              </h1>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {messages.length > 0 && (
              <button
                onClick={clearChat}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-slate-600 hover:text-slate-800
                         hover:bg-slate-100 rounded-lg transition-colors"
                aria-label="Start new conversation"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
                <span className="hidden sm:inline">New Chat</span>
              </button>
            )}
            <a
              href="https://lazycloud.dev"
              className="text-sm text-slate-500 hover:text-blue-600 transition-colors hidden sm:flex items-center gap-1"
              target="_blank"
              rel="noopener noreferrer"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
              LazyCloud
            </a>
          </div>
        </div>
      </header>

      {/* Messages Area */}
      <div
        className="flex-1 overflow-y-auto"
        role="log"
        aria-live="polite"
        aria-label="Chat messages"
        aria-relevant="additions"
      >
        <div className="max-w-3xl mx-auto px-4 py-6 space-y-4">
          {messages.length === 0 ? (
            <EmptyState />
          ) : (
            messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))
          )}

          {isLoading && messages[messages.length - 1]?.role === "user" && (
            <div className="flex justify-start message-animation">
              <div className="message-bubble message-bubble-assistant">
                <TypingIndicator />
              </div>
            </div>
          )}

          <div ref={messagesEndRef} aria-hidden="true" />
        </div>
      </div>

      {/* Input Area */}
      <div className="bg-white border-t border-slate-200 sticky bottom-0">
        <form onSubmit={sendMessage} className="max-w-3xl mx-auto px-4 py-4">
          <div className="relative flex gap-3">
            <div className="flex-1 relative">
              <label htmlFor="message-input" className="sr-only">
                Type your message
              </label>
              <textarea
                ref={inputRef}
                id="message-input"
                value={input}
                onChange={handleInputChange}
                onKeyDown={handleKeyDown}
                placeholder="Type your message... (Enter to send, Shift+Enter for new line)"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl px-4 py-3 pr-12
                         text-slate-800 placeholder-slate-400 resize-none
                         input-focus-ring focus:outline-none focus:bg-white
                         min-h-[48px] max-h-[200px]"
                rows={1}
                disabled={isLoading}
                aria-describedby="char-count input-hint"
                style={{
                  height: "auto",
                  minHeight: "48px",
                }}
                onInput={(e) => {
                  const target = e.target as HTMLTextAreaElement;
                  target.style.height = "auto";
                  target.style.height = Math.min(target.scrollHeight, 200) + "px";
                }}
              />
              <div
                id="char-count"
                className={`absolute bottom-2 right-3 text-xs ${
                  charCount > MAX_CHARS * 0.9 ? "text-amber-500" : "text-slate-400"
                }`}
                aria-live="off"
              >
                {charCount > 0 && `${charCount}/${MAX_CHARS}`}
              </div>
            </div>
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="btn-primary self-end px-5 py-3 rounded-xl text-white font-medium
                       disabled:bg-slate-300 disabled:cursor-not-allowed disabled:transform-none
                       flex items-center gap-2"
              aria-label={isLoading ? "Sending message" : "Send message"}
            >
              {isLoading ? (
                <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
              ) : (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
                </svg>
              )}
              <span className="hidden sm:inline">{isLoading ? "Sending..." : "Send"}</span>
            </button>
          </div>
          <p id="input-hint" className="text-xs text-slate-400 mt-2 ml-1">
            Press <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-slate-600 font-mono text-[10px]">Enter</kbd> to send,{" "}
            <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-slate-600 font-mono text-[10px]">Shift+Enter</kbd> for new line,{" "}
            <kbd className="px-1.5 py-0.5 bg-slate-100 rounded text-slate-600 font-mono text-[10px]">Esc</kbd> to clear
          </p>
        </form>
      </div>
    </main>
  );
}
