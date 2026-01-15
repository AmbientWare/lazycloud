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

// Storage keys
const SESSION_ID_KEY = "lazycloud_chat_session_id";
const MESSAGES_KEY = "lazycloud_chat_messages";
const THEME_KEY = "lazycloud_chat_theme";

// Generate unique ID
const generateId = () => Math.random().toString(36).substring(2, 9);

// Get or create session ID
const getSessionId = (): string => {
  if (typeof window === "undefined") return generateId();
  let sessionId = localStorage.getItem(SESSION_ID_KEY);
  if (!sessionId) {
    sessionId = generateId();
    localStorage.setItem(SESSION_ID_KEY, sessionId);
  }
  return sessionId;
};

// Save messages to localStorage
const saveMessages = (messages: Message[]) => {
  if (typeof window === "undefined") return;
  localStorage.setItem(
    MESSAGES_KEY,
    JSON.stringify(
      messages.map((m) => ({
        ...m,
        timestamp: m.timestamp.toISOString(),
      }))
    )
  );
};

// Load messages from localStorage
const loadMessages = (): Message[] => {
  if (typeof window === "undefined") return [];
  try {
    const stored = localStorage.getItem(MESSAGES_KEY);
    if (!stored) return [];
    return JSON.parse(stored).map((m: { id: string; role: "user" | "assistant"; content: string; timestamp: string }) => ({
      ...m,
      timestamp: new Date(m.timestamp),
    }));
  } catch {
    return [];
  }
};

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
                 text-slate-500 hover:text-slate-700 transition-all shadow-sm
                 sm:opacity-0 sm:group-hover:opacity-100 touch-device:opacity-100"
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
          className={`message-bubble relative px-4 py-3 group ${
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
  const [sessionId, setSessionId] = useState<string>("");
  const [isDarkMode, setIsDarkMode] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const userScrolledRef = useRef(false);

  const MAX_CHARS = 4000;

  // Load session, messages, and theme on mount
  useEffect(() => {
    setSessionId(getSessionId());
    const savedMessages = loadMessages();
    if (savedMessages.length > 0) {
      setMessages(savedMessages);
    }

    // Load theme preference
    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme) {
      setIsDarkMode(savedTheme === "dark");
    } else {
      // Check system preference
      setIsDarkMode(window.matchMedia("(prefers-color-scheme: dark)").matches);
    }
  }, []);

  // Apply dark mode class to document
  useEffect(() => {
    if (isDarkMode) {
      document.documentElement.classList.add("dark");
      document.documentElement.classList.remove("light");
    } else {
      document.documentElement.classList.remove("dark");
      document.documentElement.classList.add("light");
    }
  }, [isDarkMode]);

  const toggleDarkMode = () => {
    setIsDarkMode((prev) => {
      const newMode = !prev;
      localStorage.setItem(THEME_KEY, newMode ? "dark" : "light");
      return newMode;
    });
  };

  // Save messages when they change
  useEffect(() => {
    if (messages.length > 0) {
      saveMessages(messages);
    }
  }, [messages]);

  // Smart auto-scroll: only scroll if user is near bottom
  const scrollToBottom = useCallback((force = false) => {
    if (!messagesContainerRef.current || (!force && userScrolledRef.current)) return;
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  // Detect if user scrolled up
  const handleScroll = useCallback(() => {
    if (!messagesContainerRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = messagesContainerRef.current;
    const isNearBottom = scrollHeight - scrollTop - clientHeight < 100;
    userScrolledRef.current = !isNearBottom;
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
    userScrolledRef.current = false; // Reset scroll lock when sending

    // Timeout for streaming (5 minutes)
    const STREAM_TIMEOUT = 5 * 60 * 1000;
    let timeoutId: NodeJS.Timeout | null = null;
    const controller = new AbortController();

    try {
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

      // Set up timeout
      timeoutId = setTimeout(() => {
        controller.abort();
      }, STREAM_TIMEOUT);

      const response = await fetch(`${apiUrl}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
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

      // Add initial empty assistant message
      setMessages([...newMessages, {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        timestamp: new Date(),
      }]);

      const decoder = new TextDecoder();

      // Stream reading loop with error handling
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          const chunk = decoder.decode(value);
          accumulatedContent += chunk;

          // Immutable state update - create new message object each time
          setMessages([...newMessages, {
            id: assistantMessageId,
            role: "assistant",
            content: accumulatedContent,
            timestamp: new Date(),
          }]);
        }
      } catch (streamError) {
        // Handle stream interruption gracefully
        if (accumulatedContent) {
          // Keep partial response if we got some content
          setMessages([...newMessages, {
            id: assistantMessageId,
            role: "assistant",
            content: accumulatedContent + "\n\n[Response interrupted]",
            timestamp: new Date(),
          }]);
        }
        throw streamError;
      }
    } catch (error) {
      console.error("Error:", error);
      const errorMessage = error instanceof Error && error.name === "AbortError"
        ? "Request timed out. Please try again."
        : "Something went wrong. Please check your connection and try again.";
      addToast(errorMessage, "error");

      // Only add error message if we don't already have a partial response
      setMessages((currentMessages) => {
        const lastMessage = currentMessages[currentMessages.length - 1];
        if (lastMessage?.role === "assistant" && lastMessage.content) {
          return currentMessages; // Keep partial response
        }
        return [
          ...newMessages,
          {
            id: generateId(),
            role: "assistant",
            content: "Sorry, I encountered an error. Please try again.",
            timestamp: new Date(),
          },
        ];
      });
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
      setIsLoading(false);
      inputRef.current?.focus();
    }
  };

  const clearChat = () => {
    setMessages([]);
    // Clear localStorage but keep session ID for potential history retrieval
    if (typeof window !== "undefined") {
      localStorage.removeItem(MESSAGES_KEY);
    }
    addToast("Conversation cleared", "success");
    inputRef.current?.focus();
  };

  return (
    <main id="main-content" className="min-h-screen flex flex-col bg-[var(--color-bg-primary)]">
      {/* Toast Container */}
      <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
        {toasts.map((toast) => (
          <Toast key={toast.id} toast={toast} onDismiss={() => removeToast(toast.id)} />
        ))}
      </div>

      {/* Header */}
      <header className="bg-[var(--color-bg-secondary)] border-b border-[var(--color-border)] sticky top-0 z-40">
        <div className="max-w-3xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 bg-gradient-to-br from-blue-500 to-blue-600 rounded-xl flex items-center justify-center shadow-sm">
              <svg className="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <div>
              <h1 className="text-lg font-semibold text-[var(--color-text-primary)]">
                LazyCloud <span className="text-[var(--color-accent-primary)]">Chat</span>
              </h1>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Dark mode toggle */}
            <button
              onClick={toggleDarkMode}
              className="p-2 rounded-lg text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]
                       hover:bg-[var(--color-bg-tertiary)] transition-colors"
              aria-label={isDarkMode ? "Switch to light mode" : "Switch to dark mode"}
            >
              {isDarkMode ? (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
                </svg>
              ) : (
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
                </svg>
              )}
            </button>
            {messages.length > 0 && (
              <button
                onClick={clearChat}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]
                         hover:bg-[var(--color-bg-tertiary)] rounded-lg transition-colors"
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
              className="text-sm text-[var(--color-text-muted)] hover:text-[var(--color-accent-primary)] transition-colors hidden sm:flex items-center gap-1"
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
        ref={messagesContainerRef}
        onScroll={handleScroll}
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
      <div className="bg-[var(--color-bg-secondary)] border-t border-[var(--color-border)] sticky bottom-0">
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
                className="w-full bg-[var(--color-bg-tertiary)] border border-[var(--color-border)] rounded-xl px-4 py-3 pr-12
                         text-[var(--color-text-primary)] placeholder-[var(--color-text-muted)] resize-none
                         input-focus-ring focus:outline-none focus:bg-[var(--color-bg-secondary)]
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
                  charCount > MAX_CHARS * 0.9 ? "text-amber-500" : "text-[var(--color-text-muted)]"
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
          <p id="input-hint" className="text-xs text-[var(--color-text-muted)] mt-2 ml-1">
            Press <kbd className="px-1.5 py-0.5 bg-[var(--color-bg-tertiary)] rounded text-[var(--color-text-secondary)] font-mono text-[10px]">Enter</kbd> to send,{" "}
            <kbd className="px-1.5 py-0.5 bg-[var(--color-bg-tertiary)] rounded text-[var(--color-text-secondary)] font-mono text-[10px]">Shift+Enter</kbd> for new line,{" "}
            <kbd className="px-1.5 py-0.5 bg-[var(--color-bg-tertiary)] rounded text-[var(--color-text-secondary)] font-mono text-[10px]">Esc</kbd> to clear
          </p>
        </form>
      </div>
    </main>
  );
}
