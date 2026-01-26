import { useState, type MouseEvent } from 'react'
import {
  Check,
  ChevronsUpDown,
  Plus,
  Trash2,
  MoreHorizontal,
  LogOut,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from '@/components/ui/command'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/ui/popover'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { Workspace } from '@/interfaces/workspaces'
import { WorkspaceRoles } from '@/interfaces/workspaces'
import {
  getWorkspaces,
  createWorkspace,
  deleteWorkspace,
  leaveWorkspace,
} from '@/server/functions/workspaces'
import { DeleteDialog } from './delete-workspace-dialog'
import { CreateDialog } from './create-workspace-dialog'

interface WorkspaceSelectorProps {
  initialWorkspaces: Workspace[]
  currentWorkspaceId?: string
  onWorkspaceChange: (workspaceId: string) => void
  onWorkspacesUpdate?: (workspaces: Workspace[]) => void
}

export function WorkspaceSelector({
  initialWorkspaces,
  currentWorkspaceId,
  onWorkspaceChange,
  onWorkspacesUpdate,
}: WorkspaceSelectorProps) {
  const [open, setOpen] = useState(false)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [leaveDialogOpen, setLeaveDialogOpen] = useState(false)
  const [workspaceToDelete, setWorkspaceToDelete] = useState<Workspace | null>(
    null,
  )
  const [workspaceToLeave, setWorkspaceToLeave] = useState<Workspace | null>(
    null,
  )

  // Find current workspace - use props directly, no complex fallback logic
  const currentWorkspace = currentWorkspaceId
    ? initialWorkspaces.find((w) => w.id === currentWorkspaceId)
    : (initialWorkspaces.find((w) => w.is_personal) ?? initialWorkspaces[0])

  // Group workspaces by ownership
  const ownedWorkspaces = initialWorkspaces.filter(
    (w) => w.role === WorkspaceRoles.OWNER,
  )
  const memberWorkspaces = initialWorkspaces.filter(
    (w) => w.role !== WorkspaceRoles.OWNER,
  )

  const handleSelect = (workspaceName: string) => {
    setOpen(false)
    const selectedWorkspace = initialWorkspaces.find(
      (w) => w.name.toLowerCase() === workspaceName.toLowerCase(),
    )
    if (selectedWorkspace) {
      onWorkspaceChange(selectedWorkspace.id)
    }
  }

  const handleCreateWorkspace = () => {
    setOpen(false)
    setDialogOpen(true)
  }

  const handleSubmitCreate = async (name: string) => {
    const newWorkspace = await createWorkspace({
      data: { name },
    })
    const updatedWorkspaces = await getWorkspaces()
    onWorkspacesUpdate?.(updatedWorkspaces)
    if (newWorkspace) {
      onWorkspaceChange(newWorkspace.id)
    }
  }

  const handleDeleteWorkspace = (workspace: Workspace, e: MouseEvent) => {
    e.stopPropagation()
    setWorkspaceToDelete(workspace)
    setDeleteDialogOpen(true)
  }

  const handleLeaveWorkspace = (workspace: Workspace, e: MouseEvent) => {
    e.stopPropagation()
    setWorkspaceToLeave(workspace)
    setLeaveDialogOpen(true)
  }

  const handleConfirmDelete = async () => {
    if (!workspaceToDelete) return

    await deleteWorkspace({
      data: { workspaceId: workspaceToDelete.id },
    })
    const updatedWorkspaces = await getWorkspaces()
    onWorkspacesUpdate?.(updatedWorkspaces)

    if (currentWorkspace?.id === workspaceToDelete.id) {
      const updatedNonPersonal = updatedWorkspaces.filter((w) => !w.is_personal)
      if (updatedNonPersonal.length > 0 && updatedNonPersonal[0]) {
        onWorkspaceChange(updatedNonPersonal[0].id)
      } else if (updatedWorkspaces[0]) {
        onWorkspaceChange(updatedWorkspaces[0].id)
      }
    }

    setWorkspaceToDelete(null)
  }

  const handleConfirmLeave = async () => {
    if (!workspaceToLeave) return

    await leaveWorkspace({
      data: { workspaceId: workspaceToLeave.id },
    })
    const updatedWorkspaces = await getWorkspaces()
    onWorkspacesUpdate?.(updatedWorkspaces)

    if (currentWorkspace?.id === workspaceToLeave.id) {
      const updatedNonPersonal = updatedWorkspaces.filter((w) => !w.is_personal)
      if (updatedNonPersonal.length > 0 && updatedNonPersonal[0]) {
        onWorkspaceChange(updatedNonPersonal[0].id)
      } else if (updatedWorkspaces[0]) {
        onWorkspaceChange(updatedWorkspaces[0].id)
      }
    }

    setWorkspaceToLeave(null)
  }

  return (
    <>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            role="combobox"
            aria-expanded={open}
            className="w-full cursor-pointer justify-between sm:w-[280px]"
          >
            <span className="min-w-0 flex-1 truncate text-left">
              {initialWorkspaces.length === 0
                ? 'Personal'
                : (currentWorkspace?.name ??
                  initialWorkspaces[0]?.name ??
                  'Personal')}
            </span>
            <ChevronsUpDown className="ml-2 size-4 shrink-0 opacity-50" />
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[--radix-popover-trigger-width] p-0">
          <Command>
            <CommandInput placeholder="Search workspace..." className="h-9" />
            <CommandList>
              <CommandEmpty>No workspace found.</CommandEmpty>
              {ownedWorkspaces.length > 0 && (
                <CommandGroup heading="Owned">
                  {ownedWorkspaces.map((workspace) => (
                    <CommandItem
                      key={workspace.id}
                      value={workspace.name}
                      onSelect={handleSelect}
                      className="flex cursor-pointer items-center justify-between pr-2"
                    >
                      <div className="flex min-w-0 flex-1 items-center">
                        <span className="truncate">{workspace.name}</span>
                        <Check
                          className={cn(
                            'ml-2 size-4 shrink-0',
                            currentWorkspace?.id === workspace.id
                              ? 'opacity-100'
                              : 'opacity-0',
                          )}
                        />
                      </div>
                      {!workspace.is_personal &&
                        workspace.role === WorkspaceRoles.OWNER && (
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              asChild
                              onClick={(e: MouseEvent) => e.stopPropagation()}
                            >
                              <Button
                                variant="ghost"
                                size="icon"
                                className="ml-2 size-11 shrink-0 cursor-pointer hover:bg-primary/10"
                                aria-label="Workspace options"
                              >
                                <MoreHorizontal className="size-4" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                className="cursor-pointer text-red-600 focus:text-red-600"
                                onClick={(e: React.MouseEvent) =>
                                  handleDeleteWorkspace(workspace, e)
                                }
                              >
                                <Trash2 className="mr-2 size-4" />
                                Delete workspace
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        )}
                    </CommandItem>
                  ))}
                </CommandGroup>
              )}
              {memberWorkspaces.length > 0 && (
                <>
                  {ownedWorkspaces.length > 0 && <CommandSeparator />}
                  <CommandGroup heading="Member">
                    {memberWorkspaces.map((workspace) => (
                      <CommandItem
                        key={workspace.id}
                        value={workspace.name}
                        onSelect={handleSelect}
                        className="flex cursor-pointer items-center justify-between pr-2"
                      >
                        <div className="flex min-w-0 flex-1 items-center">
                          <span className="truncate">{workspace.name}</span>
                          <Check
                            className={cn(
                              'ml-2 size-4 shrink-0',
                              currentWorkspace?.id === workspace.id
                                ? 'opacity-100'
                                : 'opacity-0',
                            )}
                          />
                        </div>
                        {!workspace.is_personal && (
                          <DropdownMenu>
                            <DropdownMenuTrigger
                              asChild
                              onClick={(e: MouseEvent) => e.stopPropagation()}
                            >
                              <Button
                                variant="ghost"
                                size="icon"
                                className="ml-2 size-11 shrink-0 cursor-pointer hover:bg-primary/10"
                                aria-label="Workspace options"
                              >
                                <MoreHorizontal className="size-4" />
                              </Button>
                            </DropdownMenuTrigger>
                            <DropdownMenuContent align="end">
                              <DropdownMenuItem
                                className="cursor-pointer text-red-600 focus:text-red-600"
                                onClick={(e: React.MouseEvent) =>
                                  handleLeaveWorkspace(workspace, e)
                                }
                              >
                                <LogOut className="mr-2 size-4" />
                                Leave workspace
                              </DropdownMenuItem>
                            </DropdownMenuContent>
                          </DropdownMenu>
                        )}
                      </CommandItem>
                    ))}
                  </CommandGroup>
                </>
              )}
              <CommandSeparator />
              <CommandGroup>
                <CommandItem
                  onSelect={handleCreateWorkspace}
                  className="cursor-pointer"
                >
                  <Plus className="mr-2 size-4" />
                  <span>Create workspace</span>
                </CommandItem>
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>

      <CreateDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onConfirm={handleSubmitCreate}
        title="Create workspace"
        description="Create a new workspace to collaborate with your team."
        inputLabel="Workspace name"
        inputPlaceholder="My Team"
        confirmText="Create workspace"
      />

      <DeleteDialog
        open={deleteDialogOpen}
        onOpenChange={setDeleteDialogOpen}
        onConfirm={handleConfirmDelete}
        title="Delete workspace"
        description={`Are you sure you want to delete "${workspaceToDelete?.name}"? This will delete all deployments and their resources. Cleanup may take a few minutes. This action cannot be undone.`}
        itemName={workspaceToDelete?.name}
        requireConfirmation={true}
      />

      <DeleteDialog
        open={leaveDialogOpen}
        onOpenChange={setLeaveDialogOpen}
        onConfirm={handleConfirmLeave}
        title="Leave workspace"
        description={`Are you sure you want to leave "${workspaceToLeave?.name}"? You will lose access to all deployments and resources in this workspace.`}
        itemName={workspaceToLeave?.name}
        requireConfirmation={false}
        confirmText="Leave"
        loadingText="Leaving..."
      />
    </>
  )
}
