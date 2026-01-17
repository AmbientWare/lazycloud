"use client";

import { Button } from "@/components/ui/button";
import {
  Sidebar,
  SidebarContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarMenuAction,
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
import { Plus, Trash2, MessageSquare } from "lucide-react";
import type { ChatSession } from "@/lib/chat-storage";

interface AppSidebarProps {
  chats: ChatSession[];
  currentChatId: string | null;
  onSelectChat: (id: string) => void;
  onDeleteChat: (id: string) => void;
  onNewChat: () => void;
}

export function AppSidebar({
  chats,
  currentChatId,
  onSelectChat,
  onDeleteChat,
  onNewChat,
}: AppSidebarProps) {
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
