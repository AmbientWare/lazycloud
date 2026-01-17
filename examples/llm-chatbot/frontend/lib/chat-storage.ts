export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  timestamp: Date;
}

export interface ChatSession {
  id: string;
  title: string;
  messages: Message[];
  createdAt: Date;
  updatedAt: Date;
}

const CHATS_KEY = "lazycloud_chats";
const CURRENT_CHAT_KEY = "lazycloud_current_chat";

export const generateId = () => Math.random().toString(36).substring(2, 9);

export const generateTitle = (messages: Message[]): string => {
  const firstUserMessage = messages.find((m) => m.role === "user");
  if (firstUserMessage) {
    const content = firstUserMessage.content;
    return content.length > 40 ? content.substring(0, 40) + "..." : content;
  }
  return "New Chat";
};

export const saveChats = (chats: ChatSession[]) => {
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

export const loadChats = (): ChatSession[] => {
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

export const saveCurrentChatId = (chatId: string) => {
  if (typeof window === "undefined") return;
  localStorage.setItem(CURRENT_CHAT_KEY, chatId);
};

export const loadCurrentChatId = (): string | null => {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(CURRENT_CHAT_KEY);
};

export const clearChatStorage = () => {
  if (typeof window === "undefined") return;
  localStorage.removeItem(CHATS_KEY);
  localStorage.removeItem(CURRENT_CHAT_KEY);
};
