"use client";

import { useState, useTransition, useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Check, ChevronsUpDown, UserPlus, Shield, User } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Workspace, WorkspaceRole } from "@/interfaces/workspaces";
import { canInviteMembers, WorkspaceRoles } from "@/interfaces/workspaces";
import { inviteUser } from "@/actions/workspaces";
import { toast } from "sonner";

interface InviteMembersDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  workspaces: Workspace[];
  currentWorkspaceId?: string;
  currentUserRole?: WorkspaceRole;
  onReload?: () => void;
}

export function InviteMembersDialog({
  open,
  onOpenChange,
  workspaces,
  currentWorkspaceId,
  currentUserRole,
  onReload,
}: InviteMembersDialogProps) {
  const router = useRouter();
  const [, startTransition] = useTransition();
  const [email, setEmail] = useState("");
  const [selectedWorkspaceIds, setSelectedWorkspaceIds] = useState<string[]>(
    [],
  );
  const [role, setRole] = useState<WorkspaceRole>(WorkspaceRoles.MEMBER);
  const [isInviting, setIsInviting] = useState(false);
  const [popoverOpen, setPopoverOpen] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const nonPersonalWorkspaces = workspaces.filter((w) => !w.is_personal);
  const canInvite = canInviteMembers(currentUserRole);

  useEffect(() => {
    if (open && currentWorkspaceId) {
      setSelectedWorkspaceIds([currentWorkspaceId]);
    } else if (open) {
      setSelectedWorkspaceIds([]);
    }
    if (!open) {
      setEmail("");
      setRole(WorkspaceRoles.MEMBER);
      setErrors({});
    }
  }, [open, currentWorkspaceId]);

  const toggleWorkspace = (workspaceId: string) => {
    setSelectedWorkspaceIds((prev) =>
      prev.includes(workspaceId)
        ? prev.filter((id) => id !== workspaceId)
        : [...prev, workspaceId],
    );
  };

  const handleInvite = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!email.trim()) {
      setErrors({ email: "Email is required" });
      return;
    }

    if (selectedWorkspaceIds.length === 0) {
      setErrors({ workspaces: "Select at least one workspace" });
      return;
    }

    setIsInviting(true);
    setErrors({});

    const results: Array<{
      workspaceId: string;
      success: boolean;
      error?: string;
    }> = [];

    for (const workspaceId of selectedWorkspaceIds) {
      try {
        await inviteUser(workspaceId, email.trim(), role);
        results.push({ workspaceId, success: true });
      } catch (error) {
        const errorMessage =
          error instanceof Error ? error.message : "Failed to invite user";
        results.push({ workspaceId, success: false, error: errorMessage });
      }
    }

    const successful = results.filter((r) => r.success).length;
    const failed = results.filter((r) => !r.success);

    if (failed.length > 0) {
      const failedWorkspaces = failed.map((f) => {
        const workspace = workspaces.find((w) => w.id === f.workspaceId);
        return workspace?.name ?? f.workspaceId;
      });
      setErrors({
        general: `Failed to invite to: ${failedWorkspaces.join(", ")}`,
      });
      toast.error(`Failed to invite to: ${failedWorkspaces.join(", ")}`);
    }

    if (successful > 0) {
      const successfulWorkspaces = results
        .filter((r) => r.success)
        .map((r) => {
          const workspace = workspaces.find((w) => w.id === r.workspaceId);
          return workspace?.name ?? r.workspaceId;
        });

      const message =
        successfulWorkspaces.length === 1
          ? `Invitation sent to ${email.trim()} for ${successfulWorkspaces[0]}`
          : `Invitations sent to ${email.trim()} for ${successfulWorkspaces.length} workspace${successfulWorkspaces.length > 1 ? "s" : ""}`;

      toast.success(message);
      setEmail("");
      setSelectedWorkspaceIds(currentWorkspaceId ? [currentWorkspaceId] : []);
      setRole(WorkspaceRoles.MEMBER);
      onOpenChange(false);
      if (onReload) {
        onReload();
      } else {
        startTransition(() => {
          router.refresh();
        });
      }
    }

    setIsInviting(false);
  };

  const handleCancel = () => {
    onOpenChange(false);
    setEmail("");
    setSelectedWorkspaceIds(currentWorkspaceId ? [currentWorkspaceId] : []);
    setRole("member");
    setErrors({});
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px] [&>button[data-slot='dialog-close']]:cursor-pointer">
        <form onSubmit={handleInvite}>
          <DialogHeader>
            <DialogTitle>Invite Members</DialogTitle>
            <DialogDescription>
              Invite users to workspaces by email
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                placeholder="user@example.com"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  setErrors((prev) => ({ ...prev, email: "" }));
                }}
                disabled={isInviting}
                autoFocus
              />
              {errors.email && (
                <p className="text-sm text-red-500">{errors.email}</p>
              )}
            </div>

            <div className="grid gap-2">
              <Label>Workspaces</Label>
              <Popover open={popoverOpen} onOpenChange={setPopoverOpen}>
                <PopoverTrigger asChild>
                  <Button
                    type="button"
                    variant="outline"
                    role="combobox"
                    aria-expanded={popoverOpen}
                    className="w-full cursor-pointer justify-between"
                    disabled={isInviting}
                  >
                    <span className="truncate">
                      {selectedWorkspaceIds.length === 0
                        ? "Select workspaces..."
                        : selectedWorkspaceIds.length === 1
                          ? nonPersonalWorkspaces.find(
                              (w) => w.id === selectedWorkspaceIds[0],
                            )?.name
                          : `${selectedWorkspaceIds.length} workspaces selected`}
                    </span>
                    <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-[--radix-popover-trigger-width] p-0">
                  <Command>
                    <CommandInput
                      placeholder="Search workspaces..."
                      className="h-9"
                    />
                    <CommandList>
                      <CommandEmpty>No workspace found.</CommandEmpty>
                      <CommandGroup>
                        {nonPersonalWorkspaces.map((workspace) => (
                          <CommandItem
                            key={workspace.id}
                            value={workspace.name}
                            onSelect={() => toggleWorkspace(workspace.id)}
                            className="cursor-pointer"
                          >
                            <Check
                              className={cn(
                                "mr-2 h-4 w-4",
                                selectedWorkspaceIds.includes(workspace.id)
                                  ? "opacity-100"
                                  : "opacity-0",
                              )}
                            />
                            {workspace.name}
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    </CommandList>
                  </Command>
                </PopoverContent>
              </Popover>
              {errors.workspaces && (
                <p className="text-sm text-red-500">{errors.workspaces}</p>
              )}
            </div>

            <div className="grid gap-3">
              <Label>Role</Label>
              <RadioGroup
                value={role}
                onValueChange={(value) => setRole(value as WorkspaceRole)}
                disabled={isInviting}
                className="flex gap-6"
              >
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value={WorkspaceRoles.MEMBER} id="role-member" className="cursor-pointer" />
                  <Label htmlFor="role-member" className="cursor-pointer font-normal flex items-center gap-1.5">
                    <User className="h-3.5 w-3.5" />
                    Member
                  </Label>
                </div>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value={WorkspaceRoles.ADMIN} id="role-admin" className="cursor-pointer" />
                  <Label htmlFor="role-admin" className="cursor-pointer font-normal flex items-center gap-1.5">
                    <Shield className="h-3.5 w-3.5" />
                    Admin
                  </Label>
                </div>
              </RadioGroup>
              <div className="rounded-md border border-blue-500/30 bg-blue-500/5 px-3 py-2">
                <p className="text-xs text-muted-foreground">
                  {role === WorkspaceRoles.ADMIN ? (
                    <>
                      <span className="font-medium text-blue-600 dark:text-blue-400">Admins</span> can create/manage deployments and invite members
                    </>
                  ) : (
                    <>
                      <span className="font-medium text-blue-600 dark:text-blue-400">Members</span> can view deployments and monitor services
                    </>
                  )}
                </p>
              </div>
            </div>

            {errors.general && (
              <div className="rounded-md border border-red-500/30 bg-red-500/10 p-3">
                <p className="text-sm text-red-500">{errors.general}</p>
              </div>
            )}

            {!canInvite && (
              <p className="text-muted-foreground text-center text-sm">
                You need owner or admin permissions to invite members
              </p>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleCancel}
              disabled={isInviting}
              className="cursor-pointer"
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                isInviting ||
                !email.trim() ||
                selectedWorkspaceIds.length === 0 ||
                !canInvite
              }
              className="cursor-pointer"
            >
              <UserPlus className="mr-2 h-4 w-4" />
              {isInviting ? "Inviting..." : "Invite"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
