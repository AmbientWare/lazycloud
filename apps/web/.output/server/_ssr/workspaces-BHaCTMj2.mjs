import { r as reactExports, j as jsxRuntimeExports } from "../_chunks/_libs/react.mjs";
import { S as Spinner, a as StyledTooltip } from "./styled-tooltip-B5KcUPP5.mjs";
import { ag as Route$4, P as Popover, A as PopoverTrigger, B as Button, C as PopoverContent, D as Command, E as CommandInput, F as CommandList, G as CommandEmpty, H as CommandGroup, I as CommandItem, f as cn, J as CommandSeparator, ah as getWorkspaceWithDeployments, t as StyledCard, _ as StyledCardHeader, $ as SectionHeader, v as StyledCardContent, Z as DeploymentsSkeleton, a0 as Accordion, K as createWorkspace, L as getWorkspaces, M as deleteWorkspace, N as leaveWorkspace, ad as getPendingInvitations, ae as declineInvitation, af as acceptInvitation, a1 as getDeploymentStatus, W as StyledAccordionItem, X as StyledAccordionTrigger, a as Badge, Y as StyledAccordionContent, a9 as getCurrentUserInternalId, aa as getWorkspaceMembers, ab as SectionDivider, O as Drawer, Q as DrawerContent, U as DrawerHeader, V as DrawerTitle, ac as getPendingOwnershipTransfer, S as Skeleton, a4 as RadioGroup, a5 as RadioGroupItem, a2 as updateMemberRole, a3 as removeMember, a6 as inviteUser, a7 as cancelInvitation, a8 as transferOwnership } from "./router-9CFt_0DZ.mjs";
import { D as DropdownMenu, a as DropdownMenuTrigger, b as DropdownMenuContent, c as DropdownMenuItem, d as DropdownMenuSeparator, e as Select, f as SelectTrigger, g as SelectValue, h as SelectContent, i as SelectItem, S as Separator, E as ExpandConfirmButton } from "./expand-confirm-button-hfC0dEo-.mjs";
import { W as WorkspaceRoles, d as canManageMembers, e as canInviteMembers } from "./auth-CoRbzC04.mjs";
import { D as Dialog, a as DialogContent, b as DialogHeader, c as DialogTitle, d as DialogDescription, L as Label, e as DialogFooter } from "./label-D0TBbP5s.mjs";
import { I as Input } from "./input-DCy5eMTd.mjs";
import { e as useRouter } from "../_chunks/_libs/@tanstack/react-router.mjs";
import { t as toast } from "../_libs/sonner.mjs";
import { B as Building2, z as ChevronsUpDown, e as Check, J as Ellipsis, N as Trash2, n as LogOut, Q as Plus, X, U as Crown, L as LoaderCircle, c as CircleX, V as Rocket, a as CircleAlert, W as Server, Y as HardDrive, _ as Network, I as Info, E as ExternalLink, $ as EllipsisVertical, a0 as UserPlus, y as Mail, a1 as Users, T as TriangleAlert, p as Copy, a2 as Cpu, u as Activity, a3 as Scaling, a4 as Shield, a5 as User, a6 as Send } from "../_libs/lucide-react.mjs";
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "../_chunks/_libs/@tanstack/react-router-ssr-query.mjs";
import "../_chunks/_libs/@tanstack/react-query.mjs";
import "../_chunks/_libs/@tanstack/router-ssr-query-core.mjs";
import "../_chunks/_libs/@tanstack/query-core.mjs";
import "../_libs/superjson.mjs";
import "../_libs/copy-anything.mjs";
import "../_libs/is-what.mjs";
import "./index.mjs";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
import "../_chunks/_libs/@radix-ui/react-tooltip.mjs";
import "../_chunks/_libs/@radix-ui/primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-compose-refs.mjs";
import "../_chunks/_libs/@radix-ui/react-context.mjs";
import "../_chunks/_libs/@radix-ui/react-dismissable-layer.mjs";
import "../_chunks/_libs/@radix-ui/react-primitive.mjs";
import "../_chunks/_libs/@radix-ui/react-slot.mjs";
import "../_chunks/_libs/@radix-ui/react-use-callback-ref.mjs";
import "../_chunks/_libs/@radix-ui/react-use-escape-keydown.mjs";
import "../_chunks/_libs/@radix-ui/react-id.mjs";
import "../_chunks/_libs/@radix-ui/react-use-layout-effect.mjs";
import "../_chunks/_libs/@radix-ui/react-popper.mjs";
import "../_chunks/_libs/@floating-ui/react-dom.mjs";
import "../_chunks/_libs/@floating-ui/dom.mjs";
import "../_chunks/_libs/@floating-ui/core.mjs";
import "../_chunks/_libs/@floating-ui/utils.mjs";
import "../_chunks/_libs/@radix-ui/react-arrow.mjs";
import "../_chunks/_libs/@radix-ui/react-use-size.mjs";
import "../_chunks/_libs/@radix-ui/react-portal.mjs";
import "../_chunks/_libs/@radix-ui/react-presence.mjs";
import "../_chunks/_libs/@radix-ui/react-use-controllable-state.mjs";
import "../_chunks/_libs/@radix-ui/react-visually-hidden.mjs";
import "../_libs/clsx.mjs";
import "../_libs/tailwind-merge.mjs";
import "../_chunks/_libs/@radix-ui/react-direction.mjs";
import "../_libs/next-themes.mjs";
import "../_libs/fumadocs-mdx.mjs";
import "node:path";
import "../_libs/class-variance-authority.mjs";
import "../_chunks/_libs/@radix-ui/react-tabs.mjs";
import "../_chunks/_libs/@radix-ui/react-roving-focus.mjs";
import "../_chunks/_libs/@radix-ui/react-collection.mjs";
import "./source-Zpe9Usb2.mjs";
import "../_libs/cmdk.mjs";
import "../_chunks/_libs/@radix-ui/react-dialog.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-scope.mjs";
import "../_chunks/_libs/@radix-ui/react-focus-guards.mjs";
import "../_libs/react-remove-scroll.mjs";
import "../_libs/tslib.mjs";
import "../_libs/react-remove-scroll-bar.mjs";
import "../_libs/react-style-singleton.mjs";
import "../_libs/get-nonce.mjs";
import "../_libs/use-sidecar.mjs";
import "../_libs/use-callback-ref.mjs";
import "../_libs/aria-hidden.mjs";
import "../_chunks/_libs/@radix-ui/react-popover.mjs";
import "../_chunks/_libs/@radix-ui/react-accordion.mjs";
import "../_chunks/_libs/@radix-ui/react-collapsible.mjs";
import "../_libs/vaul.mjs";
import "../_chunks/_libs/@radix-ui/react-radio-group.mjs";
import "../_chunks/_libs/@radix-ui/react-use-previous.mjs";
import "../_libs/date-fns.mjs";
import "../_chunks/_libs/@orama/orama.mjs";
import "../_libs/zod.mjs";
import "./env-vgS3Y8Xp.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@radix-ui/react-dropdown-menu.mjs";
import "../_chunks/_libs/@radix-ui/react-menu.mjs";
import "../_chunks/_libs/@radix-ui/react-separator.mjs";
import "../_chunks/_libs/@radix-ui/react-select.mjs";
import "../_chunks/_libs/@radix-ui/number.mjs";
import "../_chunks/_libs/@radix-ui/react-label.mjs";
function DeleteDialog({
  open,
  onOpenChange,
  onConfirm,
  title,
  description,
  itemName,
  requireConfirmation = false,
  confirmText = "Delete",
  loadingText = "Deleting..."
}) {
  const [isDeleting, setIsDeleting] = reactExports.useState(false);
  const [confirmationInput, setConfirmationInput] = reactExports.useState("");
  const canDelete = !requireConfirmation || confirmationInput === itemName;
  const handleConfirm = async () => {
    if (!canDelete) return;
    setIsDeleting(true);
    try {
      await onConfirm();
      onOpenChange(false);
      setConfirmationInput("");
    } catch (error) {
      console.error("Delete failed:", error);
    } finally {
      setIsDeleting(false);
    }
  };
  const handleCancel = () => {
    onOpenChange(false);
    setConfirmationInput("");
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Dialog, { open, onOpenChange, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogContent, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(CircleAlert, { className: "size-5 text-red-500" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(DialogTitle, { children: title })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogDescription, { className: "pt-2", children: description })
    ] }),
    requireConfirmation && itemName && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-2 py-4", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs(Label, { htmlFor: "confirm", children: [
        "Type ",
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-semibold", children: itemName }),
        " to confirm"
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Input,
        {
          id: "confirm",
          value: confirmationInput,
          onChange: (e) => setConfirmationInput(e.target.value),
          placeholder: itemName,
          disabled: isDeleting,
          autoFocus: true
        }
      )
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogFooter, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "outline",
          onClick: handleCancel,
          disabled: isDeleting,
          className: "cursor-pointer transition-all duration-200 hover:bg-accent/50 hover:border-border hover:-translate-y-0.5 active:translate-y-0",
          children: "Cancel"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "destructive",
          onClick: handleConfirm,
          disabled: isDeleting || !canDelete,
          className: "cursor-pointer transition-all duration-200 hover:bg-destructive/90 hover:shadow-md hover:shadow-destructive/20 hover:-translate-y-0.5 active:translate-y-0 active:scale-[0.98]",
          children: isDeleting ? /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "mr-2 size-4 animate-spin" }),
            loadingText
          ] }) : confirmText
        }
      )
    ] })
  ] }) });
}
function CreateDialog({
  open,
  onOpenChange,
  onConfirm,
  title,
  description,
  inputLabel,
  inputPlaceholder,
  confirmText = "Create"
}) {
  const [isCreating, setIsCreating] = reactExports.useState(false);
  const [name, setName] = reactExports.useState("");
  const handleConfirm = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setIsCreating(true);
    try {
      await onConfirm(name.trim());
      onOpenChange(false);
      setName("");
    } catch (error) {
      console.error("Create failed:", error);
    } finally {
      setIsCreating(false);
    }
  };
  const handleCancel = () => {
    onOpenChange(false);
    setName("");
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Dialog, { open, onOpenChange, children: /* @__PURE__ */ jsxRuntimeExports.jsx(DialogContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("form", { onSubmit: handleConfirm, children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogTitle, { children: title }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogDescription, { children: description })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "grid gap-4 py-4", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-2", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(Label, { htmlFor: "name", children: inputLabel }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Input,
        {
          id: "name",
          placeholder: inputPlaceholder,
          value: name,
          onChange: (e) => setName(e.target.value),
          disabled: isCreating,
          autoFocus: true
        }
      )
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogFooter, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "outline",
          onClick: handleCancel,
          disabled: isCreating,
          className: "cursor-pointer",
          children: "Cancel"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "submit",
          disabled: isCreating || !name.trim(),
          className: "cursor-pointer",
          children: isCreating ? "Creating..." : confirmText
        }
      )
    ] })
  ] }) }) });
}
function WorkspaceSelector({
  initialWorkspaces,
  currentWorkspaceId,
  onWorkspaceChange,
  onWorkspacesUpdate
}) {
  const [open, setOpen] = reactExports.useState(false);
  const [dialogOpen, setDialogOpen] = reactExports.useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = reactExports.useState(false);
  const [leaveDialogOpen, setLeaveDialogOpen] = reactExports.useState(false);
  const [workspaceToDelete, setWorkspaceToDelete] = reactExports.useState(
    null
  );
  const [workspaceToLeave, setWorkspaceToLeave] = reactExports.useState(
    null
  );
  const currentWorkspace = currentWorkspaceId ? initialWorkspaces.find((w) => w.id === currentWorkspaceId) : initialWorkspaces.find((w) => w.is_personal) ?? initialWorkspaces[0];
  const ownedWorkspaces = initialWorkspaces.filter(
    (w) => w.role === WorkspaceRoles.OWNER
  );
  const memberWorkspaces = initialWorkspaces.filter(
    (w) => w.role !== WorkspaceRoles.OWNER
  );
  const handleSelect = (workspaceName) => {
    setOpen(false);
    const selectedWorkspace = initialWorkspaces.find(
      (w) => w.name.toLowerCase() === workspaceName.toLowerCase()
    );
    if (selectedWorkspace) {
      onWorkspaceChange(selectedWorkspace.id);
    }
  };
  const handleCreateWorkspace = () => {
    setOpen(false);
    setDialogOpen(true);
  };
  const handleSubmitCreate = async (name) => {
    const newWorkspace = await createWorkspace({
      data: { name }
    });
    const updatedWorkspaces = await getWorkspaces();
    onWorkspacesUpdate?.(updatedWorkspaces);
    if (newWorkspace) {
      onWorkspaceChange(newWorkspace.id);
    }
  };
  const handleDeleteWorkspace = (workspace, e) => {
    e.stopPropagation();
    setWorkspaceToDelete(workspace);
    setDeleteDialogOpen(true);
  };
  const handleLeaveWorkspace = (workspace, e) => {
    e.stopPropagation();
    setWorkspaceToLeave(workspace);
    setLeaveDialogOpen(true);
  };
  const handleConfirmDelete = async () => {
    if (!workspaceToDelete) return;
    await deleteWorkspace({
      data: { workspaceId: workspaceToDelete.id }
    });
    const updatedWorkspaces = await getWorkspaces();
    onWorkspacesUpdate?.(updatedWorkspaces);
    if (currentWorkspace?.id === workspaceToDelete.id) {
      const updatedNonPersonal = updatedWorkspaces.filter((w) => !w.is_personal);
      if (updatedNonPersonal.length > 0 && updatedNonPersonal[0]) {
        onWorkspaceChange(updatedNonPersonal[0].id);
      } else if (updatedWorkspaces[0]) {
        onWorkspaceChange(updatedWorkspaces[0].id);
      }
    }
    setWorkspaceToDelete(null);
  };
  const handleConfirmLeave = async () => {
    if (!workspaceToLeave) return;
    await leaveWorkspace({
      data: { workspaceId: workspaceToLeave.id }
    });
    const updatedWorkspaces = await getWorkspaces();
    onWorkspacesUpdate?.(updatedWorkspaces);
    if (currentWorkspace?.id === workspaceToLeave.id) {
      const updatedNonPersonal = updatedWorkspaces.filter((w) => !w.is_personal);
      if (updatedNonPersonal.length > 0 && updatedNonPersonal[0]) {
        onWorkspaceChange(updatedNonPersonal[0].id);
      } else if (updatedWorkspaces[0]) {
        onWorkspaceChange(updatedWorkspaces[0].id);
      }
    }
    setWorkspaceToLeave(null);
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(Popover, { open, onOpenChange: setOpen, children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(PopoverTrigger, { asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
        Button,
        {
          variant: "outline",
          role: "combobox",
          "aria-expanded": open,
          className: "w-full cursor-pointer justify-between sm:w-[280px]",
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "min-w-0 flex-1 truncate text-left", children: initialWorkspaces.length === 0 ? "Personal" : currentWorkspace?.name ?? initialWorkspaces[0]?.name ?? "Personal" }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(ChevronsUpDown, { className: "ml-2 size-4 shrink-0 opacity-50" })
          ]
        }
      ) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(PopoverContent, { className: "w-[--radix-popover-trigger-width] p-0", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Command, { children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(CommandInput, { placeholder: "Search workspace...", className: "h-9" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs(CommandList, { children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(CommandEmpty, { children: "No workspace found." }),
          ownedWorkspaces.length > 0 && /* @__PURE__ */ jsxRuntimeExports.jsx(CommandGroup, { heading: "Owned", children: ownedWorkspaces.map((workspace) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
            CommandItem,
            {
              value: workspace.name,
              onSelect: handleSelect,
              className: "flex cursor-pointer items-center justify-between pr-2",
              children: [
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-w-0 flex-1 items-center", children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: workspace.name }),
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    Check,
                    {
                      className: cn(
                        "ml-2 size-4 shrink-0",
                        currentWorkspace?.id === workspace.id ? "opacity-100" : "opacity-0"
                      )
                    }
                  )
                ] }),
                !workspace.is_personal && workspace.role === WorkspaceRoles.OWNER && /* @__PURE__ */ jsxRuntimeExports.jsxs(DropdownMenu, { children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    DropdownMenuTrigger,
                    {
                      asChild: true,
                      onClick: (e) => e.stopPropagation(),
                      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                        Button,
                        {
                          variant: "ghost",
                          size: "icon",
                          className: "ml-2 size-11 shrink-0 cursor-pointer hover:bg-primary/10",
                          "aria-label": "Workspace options",
                          children: /* @__PURE__ */ jsxRuntimeExports.jsx(Ellipsis, { className: "size-4" })
                        }
                      )
                    }
                  ),
                  /* @__PURE__ */ jsxRuntimeExports.jsx(DropdownMenuContent, { align: "end", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
                    DropdownMenuItem,
                    {
                      className: "cursor-pointer text-red-600 focus:text-red-600",
                      onClick: (e) => handleDeleteWorkspace(workspace, e),
                      children: [
                        /* @__PURE__ */ jsxRuntimeExports.jsx(Trash2, { className: "mr-2 size-4" }),
                        "Delete workspace"
                      ]
                    }
                  ) })
                ] })
              ]
            },
            workspace.id
          )) }),
          memberWorkspaces.length > 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            ownedWorkspaces.length > 0 && /* @__PURE__ */ jsxRuntimeExports.jsx(CommandSeparator, {}),
            /* @__PURE__ */ jsxRuntimeExports.jsx(CommandGroup, { heading: "Member", children: memberWorkspaces.map((workspace) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
              CommandItem,
              {
                value: workspace.name,
                onSelect: handleSelect,
                className: "flex cursor-pointer items-center justify-between pr-2",
                children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-w-0 flex-1 items-center", children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: workspace.name }),
                    /* @__PURE__ */ jsxRuntimeExports.jsx(
                      Check,
                      {
                        className: cn(
                          "ml-2 size-4 shrink-0",
                          currentWorkspace?.id === workspace.id ? "opacity-100" : "opacity-0"
                        )
                      }
                    )
                  ] }),
                  !workspace.is_personal && /* @__PURE__ */ jsxRuntimeExports.jsxs(DropdownMenu, { children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(
                      DropdownMenuTrigger,
                      {
                        asChild: true,
                        onClick: (e) => e.stopPropagation(),
                        children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                          Button,
                          {
                            variant: "ghost",
                            size: "icon",
                            className: "ml-2 size-11 shrink-0 cursor-pointer hover:bg-primary/10",
                            "aria-label": "Workspace options",
                            children: /* @__PURE__ */ jsxRuntimeExports.jsx(Ellipsis, { className: "size-4" })
                          }
                        )
                      }
                    ),
                    /* @__PURE__ */ jsxRuntimeExports.jsx(DropdownMenuContent, { align: "end", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
                      DropdownMenuItem,
                      {
                        className: "cursor-pointer text-red-600 focus:text-red-600",
                        onClick: (e) => handleLeaveWorkspace(workspace, e),
                        children: [
                          /* @__PURE__ */ jsxRuntimeExports.jsx(LogOut, { className: "mr-2 size-4" }),
                          "Leave workspace"
                        ]
                      }
                    ) })
                  ] })
                ]
              },
              workspace.id
            )) })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsx(CommandSeparator, {}),
          /* @__PURE__ */ jsxRuntimeExports.jsx(CommandGroup, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
            CommandItem,
            {
              onSelect: handleCreateWorkspace,
              className: "cursor-pointer",
              children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx(Plus, { className: "mr-2 size-4" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Create workspace" })
              ]
            }
          ) })
        ] })
      ] }) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      CreateDialog,
      {
        open: dialogOpen,
        onOpenChange: setDialogOpen,
        onConfirm: handleSubmitCreate,
        title: "Create workspace",
        description: "Create a new workspace to collaborate with your team.",
        inputLabel: "Workspace name",
        inputPlaceholder: "My Team",
        confirmText: "Create workspace"
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      DeleteDialog,
      {
        open: deleteDialogOpen,
        onOpenChange: setDeleteDialogOpen,
        onConfirm: handleConfirmDelete,
        title: "Delete workspace",
        description: `Are you sure you want to delete "${workspaceToDelete?.name}"? This will delete all deployments and their resources. Cleanup may take a few minutes. This action cannot be undone.`,
        itemName: workspaceToDelete?.name,
        requireConfirmation: true
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      DeleteDialog,
      {
        open: leaveDialogOpen,
        onOpenChange: setLeaveDialogOpen,
        onConfirm: handleConfirmLeave,
        title: "Leave workspace",
        description: `Are you sure you want to leave "${workspaceToLeave?.name}"? You will lose access to all deployments and resources in this workspace.`,
        itemName: workspaceToLeave?.name,
        requireConfirmation: false,
        confirmText: "Leave",
        loadingText: "Leaving..."
      }
    )
  ] });
}
function Table({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "div",
    {
      "data-slot": "table-container",
      className: "relative w-full overflow-x-auto",
      children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        "table",
        {
          "data-slot": "table",
          className: cn("w-full caption-bottom text-sm", className),
          ...props
        }
      )
    }
  );
}
function TableHeader({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "thead",
    {
      "data-slot": "table-header",
      className: cn("[&_tr]:border-b", className),
      ...props
    }
  );
}
function TableBody({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "tbody",
    {
      "data-slot": "table-body",
      className: cn("[&_tr:last-child]:border-0", className),
      ...props
    }
  );
}
function TableRow({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "tr",
    {
      "data-slot": "table-row",
      className: cn(
        "hover:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors",
        className
      ),
      ...props
    }
  );
}
function TableHead({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "th",
    {
      "data-slot": "table-head",
      className: cn(
        "text-foreground h-10 px-2 text-left align-middle font-medium whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      ),
      ...props
    }
  );
}
function TableCell({ className, ...props }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    "td",
    {
      "data-slot": "table-cell",
      className: cn(
        "p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]",
        className
      ),
      ...props
    }
  );
}
function getStatusColor(status) {
  switch (status.toLowerCase()) {
    case "running":
      return "bg-green-500/10 text-green-500 border-green-500/30";
    case "pending":
    case "creating":
    case "health_check":
    case "updating":
      return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
    case "error":
    case "failed":
    case "restarting":
      return "bg-red-500/10 text-red-500 border-red-500/30";
    case "exited":
    case "stopping":
      return "bg-muted text-muted-foreground border-border";
    default:
      return "";
  }
}
function formatProbeType(probe) {
  if (!probe) return "Not configured";
  if (probe.httpGet) {
    return `HTTP ${probe.httpGet.path}:${probe.httpGet.port}`;
  }
  if (probe.tcpSocket) {
    return `TCP :${probe.tcpSocket.port}`;
  }
  if (probe.exec) {
    return "Exec";
  }
  return "Configured";
}
function getStatusDisplay(service) {
  const hasHealthCheck = service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe;
  if (service.status.toLowerCase() === "running" && !hasHealthCheck) {
    return "Running (no health check)";
  }
  return service.status;
}
function SectionCard({
  icon: Icon,
  title,
  iconColor,
  children
}) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "rounded-lg border border-border/50 bg-muted/30 p-4", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-3 flex items-center gap-2", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        "div",
        {
          className: `flex size-6 items-center justify-center rounded-md ${iconColor}`,
          children: /* @__PURE__ */ jsxRuntimeExports.jsx(Icon, { className: "h-3.5 w-3.5" })
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: title })
    ] }),
    children
  ] });
}
function InfoRow({ label, value }) {
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between py-1", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: label }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs font-medium", children: value })
  ] });
}
function ServiceDetailsSheet({
  service,
  open,
  onOpenChange
}) {
  const [copied, setCopied] = reactExports.useState(false);
  if (!service) return null;
  const copyEndpoint = async () => {
    if (service.endpoint) {
      await navigator.clipboard.writeText(`https://${service.endpoint}`);
      setCopied(true);
      setTimeout(() => setCopied(false), 2e3);
    }
  };
  const hasResources = service.resources?.requests || service.resources?.limits || service.current_usage;
  const hasHealthChecks = service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe;
  const hasHPA = service.hpa?.enabled;
  const hasPods = service.pods && service.pods.length > 0;
  const getHPATargets = () => {
    if (!service.hpa?.metrics) return { cpu: null, memory: null };
    let cpu = null;
    let memory = null;
    for (const metric of service.hpa.metrics) {
      if (metric.type === "Resource" && metric.resource) {
        const name = metric.resource.name;
        const target = metric.resource.target;
        if (name === "cpu" && target?.averageUtilization) {
          cpu = target.averageUtilization;
        }
        if (name === "memory" && target?.averageUtilization) {
          memory = target.averageUtilization;
        }
      }
    }
    return { cpu, memory };
  };
  const hpaTargets = getHPATargets();
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Drawer, { open, onOpenChange, children: /* @__PURE__ */ jsxRuntimeExports.jsx(DrawerContent, { className: "max-h-[96dvh] sm:max-h-[90vh]", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mx-auto w-full max-w-3xl", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(DrawerHeader, { className: "pb-2", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Badge,
        {
          variant: "outline",
          className: `text-xs ${getStatusColor(service.status)}`,
          children: getStatusDisplay(service)
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(DrawerTitle, { children: service.name })
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(Separator, {}),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "overflow-y-auto p-4 pb-12 sm:pb-8", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 sm:gap-x-6", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "text-sm", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Replicas: " }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "font-medium", children: [
            service.ready_replicas,
            "/",
            service.total_replicas
          ] })
        ] }),
        service.ports && service.ports.length > 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "text-sm", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Ports: " }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium", children: service.ports.join(", ") })
        ] }),
        service.restarts > 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "text-sm", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Restarts: " }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium text-destructive", children: service.restarts })
        ] })
      ] }),
      service.endpoint && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mb-6 flex items-center gap-2 rounded-lg border border-border/50 bg-muted/50 p-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs(
          "a",
          {
            href: `https://${service.endpoint}`,
            target: "_blank",
            rel: "noopener noreferrer",
            className: "flex items-center gap-2 truncate text-sm text-cyan-500 transition-colors hover:text-cyan-400",
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(ExternalLink, { className: "size-4 shrink-0" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: service.endpoint })
            ]
          }
        ),
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          "button",
          {
            onClick: copyEndpoint,
            className: "ml-auto shrink-0 rounded p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
            title: "Copy endpoint",
            "aria-label": "Copy endpoint",
            children: copied ? /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "size-4 text-green-500" }) : /* @__PURE__ */ jsxRuntimeExports.jsx(Copy, { className: "size-4" })
          }
        )
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-4 sm:grid-cols-2", children: [
        hasResources && /* @__PURE__ */ jsxRuntimeExports.jsx(
          SectionCard,
          {
            icon: Cpu,
            title: "Resources",
            iconColor: "bg-blue-500/10 text-blue-500",
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden grid-cols-4 gap-2 border-b border-border/30 pb-1 text-xs text-muted-foreground sm:grid", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", {}),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Request" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Limit" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: "Usage" })
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden grid-cols-4 gap-2 py-1 text-xs sm:grid", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "CPU" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.resources?.requests?.cpu || "-" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.resources?.limits?.cpu || "-" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.current_usage?.cpu || "-" })
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden grid-cols-4 gap-2 py-1 text-xs sm:grid", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Memory" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.resources?.requests?.memory || "-" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.resources?.limits?.memory || "-" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service.current_usage?.memory || "-" })
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3 sm:hidden", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: "CPU" }),
                  /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-1 grid grid-cols-3 gap-2 text-xs", children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Req:" }),
                      " ",
                      service.resources?.requests?.cpu || "-"
                    ] }),
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Lim:" }),
                      " ",
                      service.resources?.limits?.cpu || "-"
                    ] }),
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Use:" }),
                      " ",
                      service.current_usage?.cpu || "-"
                    ] })
                  ] })
                ] }),
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: "Memory" }),
                  /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-1 grid grid-cols-3 gap-2 text-xs", children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Req:" }),
                      " ",
                      service.resources?.requests?.memory || "-"
                    ] }),
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Lim:" }),
                      " ",
                      service.resources?.limits?.memory || "-"
                    ] }),
                    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-muted-foreground", children: "Use:" }),
                      " ",
                      service.current_usage?.memory || "-"
                    ] })
                  ] })
                ] })
              ] })
            ] })
          }
        ),
        hasHealthChecks && /* @__PURE__ */ jsxRuntimeExports.jsx(
          SectionCard,
          {
            icon: Activity,
            title: "Health Checks",
            iconColor: "bg-green-500/10 text-green-500",
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-1", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                InfoRow,
                {
                  label: "Liveness",
                  value: formatProbeType(
                    service.healthcheck?.livenessProbe
                  )
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                InfoRow,
                {
                  label: "Readiness",
                  value: formatProbeType(
                    service.healthcheck?.readinessProbe
                  )
                }
              )
            ] })
          }
        ),
        hasHPA && service.hpa && /* @__PURE__ */ jsxRuntimeExports.jsx(
          SectionCard,
          {
            icon: Scaling,
            title: "Auto-scaling",
            iconColor: "bg-purple-500/10 text-purple-500",
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-wrap gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Badge,
                {
                  variant: "outline",
                  className: "border-green-500/30 bg-green-500/10 text-xs text-green-500",
                  children: "Enabled"
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-muted-foreground", children: [
                "Min:",
                " ",
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium text-foreground", children: service.hpa.minReplicas })
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-muted-foreground", children: [
                "Max:",
                " ",
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium text-foreground", children: service.hpa.maxReplicas })
              ] }),
              hpaTargets.cpu && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-muted-foreground", children: [
                "CPU:",
                " ",
                /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "font-medium text-foreground", children: [
                  hpaTargets.cpu,
                  "%"
                ] })
              ] }),
              hpaTargets.memory && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-muted-foreground", children: [
                "Mem:",
                " ",
                /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "font-medium text-foreground", children: [
                  hpaTargets.memory,
                  "%"
                ] })
              ] })
            ] })
          }
        )
      ] }),
      hasPods && service.pods && /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-4", children: /* @__PURE__ */ jsxRuntimeExports.jsx(
        SectionCard,
        {
          icon: Server,
          title: `Instances (${service.pods.length})`,
          iconColor: "bg-orange-500/10 text-orange-500",
          children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "-mx-1 overflow-x-auto", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Table, { children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(TableHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs(TableRow, { className: "border-border/30", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "Name" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "Status" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "Ready" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "CPU" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "Memory" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableHead, { className: "h-8 text-xs", children: "Restarts" })
            ] }) }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(TableBody, { children: service.pods.map((pod) => /* @__PURE__ */ jsxRuntimeExports.jsxs(TableRow, { className: "border-border/30", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                TableCell,
                {
                  className: "max-w-[150px] truncate py-2 font-mono text-xs",
                  title: pod.name,
                  children: pod.name.length > 24 ? `${pod.name.slice(0, 21)}...` : pod.name
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableCell, { className: "py-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                Badge,
                {
                  variant: "outline",
                  className: `text-xs capitalize ${getStatusColor(pod.phase)}`,
                  children: pod.phase
                }
              ) }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs(TableCell, { className: "py-2 text-xs", children: [
                pod.ready_containers,
                "/",
                pod.total_containers
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableCell, { className: "py-2 text-xs", children: pod.cpu_usage || "-" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(TableCell, { className: "py-2 text-xs", children: pod.memory_usage || "-" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                TableCell,
                {
                  className: `py-2 text-xs ${pod.restart_count > 0 ? "text-destructive" : ""}`,
                  children: pod.restart_count
                }
              )
            ] }, pod.name)) })
          ] }) })
        }
      ) }),
      !hasResources && !hasHealthChecks && !hasHPA && !hasPods && /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col items-center justify-center py-8 text-center", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mb-3 flex size-10 items-center justify-center rounded-lg bg-muted", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Server, { className: "size-5 text-muted-foreground" }) }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-muted-foreground", children: "No additional details available" })
      ] })
    ] })
  ] }) }) });
}
function DomainSetupNotice({
  customDomain,
  cnameTarget,
  domainStatus
}) {
  const [copied, setCopied] = reactExports.useState(false);
  const copyToClipboard = async () => {
    await navigator.clipboard.writeText(cnameTarget);
    setCopied(true);
    setTimeout(() => setCopied(false), 2e3);
  };
  const getStatusLabel = () => {
    switch (domainStatus) {
      case "pending_validation":
        return "Pending DNS";
      case "initializing":
        return "Initializing";
      default:
        return "SSL Pending";
    }
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-2 rounded-md border border-yellow-500/30 bg-yellow-500/5 p-2", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-start gap-2", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(TriangleAlert, { className: "mt-0.5 h-3.5 w-3.5 shrink-0 text-yellow-500" }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "min-w-0 flex-1 space-y-1.5", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex items-center gap-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs font-medium text-yellow-500", children: getStatusLabel() }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-xs text-muted-foreground", children: "Add a CNAME record to your DNS:" }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 text-xs", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("code", { className: "truncate rounded bg-muted px-1.5 py-0.5 text-foreground", children: customDomain }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "shrink-0 text-muted-foreground", children: "→" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("code", { className: "rounded bg-muted px-1.5 py-0.5 text-foreground", children: cnameTarget }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          "button",
          {
            onClick: copyToClipboard,
            className: "flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center p-2 text-muted-foreground transition-colors hover:text-foreground",
            title: "Copy CNAME target",
            "aria-label": "Copy CNAME target",
            children: copied ? /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "size-4 text-green-500" }) : /* @__PURE__ */ jsxRuntimeExports.jsx(Copy, { className: "size-4" })
          }
        )
      ] })
    ] })
  ] }) });
}
function DeploymentCard({
  deployment
}) {
  const { status, isLoading, service_count, volume_count, network_count } = deployment;
  const services = status?.services ?? [];
  const volumes = status?.volumes ?? [];
  const networks = status?.networks ?? [];
  const hasStatusData = status !== void 0 && status !== null;
  const [selectedServiceName, setSelectedServiceName] = reactExports.useState(
    null
  );
  const [sheetOpen, setSheetOpen] = reactExports.useState(false);
  const selectedService = selectedServiceName ? services.find((s) => s.name === selectedServiceName) ?? null : null;
  const openServiceDetails = (service) => {
    setSelectedServiceName(service.name);
    setSheetOpen(true);
  };
  const getStateBadgeColor = (state) => {
    switch (state.toLowerCase()) {
      case "deployed":
        return "bg-green-500/10 text-green-500 border-green-500/30";
      case "deploying":
        return "bg-blue-500/10 text-blue-500 border-blue-500/30";
      case "failed":
        return "bg-red-500/10 text-red-500 border-red-500/30";
      case "deleting":
        return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
      case "deleted":
        return "";
      default:
        return "";
    }
  };
  const getServiceStatusColor = (status2) => {
    switch (status2.toLowerCase()) {
      case "running":
        return "bg-green-500/10 text-green-500 border-green-500/30";
      case "pending":
        return "bg-yellow-500/10 text-yellow-500 border-yellow-500/30";
      case "failed":
        return "bg-red-500/10 text-red-500 border-red-500/30";
      default:
        return "";
    }
  };
  const getServiceStatusDisplay = (service) => {
    const hasHealthCheck = service.healthcheck?.livenessProbe || service.healthcheck?.readinessProbe;
    if (service.status.toLowerCase() === "running" && !hasHealthCheck) {
      return "Running (no health check)";
    }
    return service.status;
  };
  if (isLoading) {
    return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledAccordionItem, { value: deployment.id, children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(StyledAccordionTrigger, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex w-full items-center justify-between pr-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "size-2 rounded-full bg-lazycloud" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold transition-colors group-data-[state=open]:text-lazycloud", children: deployment.name }),
          /* @__PURE__ */ jsxRuntimeExports.jsx(
            Badge,
            {
              variant: "outline",
              className: `text-xs capitalize ${getStateBadgeColor(deployment.state)}`,
              children: deployment.state
            }
          )
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden items-center gap-4 text-sm text-muted-foreground sm:flex", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(Server, { className: "size-4" }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service_count })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(HardDrive, { className: "size-4" }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: volume_count })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(Network, { className: "size-4" }),
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: network_count })
          ] })
        ] })
      ] }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(StyledAccordionContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex items-center justify-center", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Spinner, { size: "md" }) }) })
    ] });
  }
  const runningServices = services.filter(
    (s) => s.status.toLowerCase() === "running"
  ).length;
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledAccordionItem, { value: deployment.id, children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledAccordionTrigger, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex w-full items-center justify-between pr-4", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "size-2 rounded-full bg-lazycloud" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-sm font-semibold transition-colors group-data-[state=open]:text-lazycloud", children: deployment.name }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          Badge,
          {
            variant: "outline",
            className: `text-xs capitalize ${getStateBadgeColor(deployment.state)}`,
            children: deployment.state
          }
        )
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "hidden items-center gap-4 text-sm text-muted-foreground sm:flex", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(Server, { className: "size-4" }),
          status ? /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { children: [
            runningServices,
            "/",
            services.length
          ] }) : /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: service_count })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(HardDrive, { className: "size-4" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: status?.volumes !== void 0 && status.volumes !== null ? volumes.length : volume_count })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-1.5", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(Network, { className: "size-4" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: status?.networks !== void 0 && status.networks !== null ? networks.length : network_count })
        ] })
      ] })
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledAccordionContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-6 pt-2", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid grid-cols-1 gap-4 sm:grid-cols-2 sm:gap-6 lg:grid-cols-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 pb-1", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-7 items-center justify-center rounded-md bg-blue-500/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Server, { className: "size-4 text-blue-500" }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Services" }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "ml-auto flex items-center gap-2", children: [
            status?.ready_services !== void 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-muted-foreground", children: [
              status.ready_services,
              "/",
              status.total_services,
              " ready"
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(Badge, { variant: "outline", className: "text-xs", children: services.length })
          ] })
        ] }),
        !hasStatusData ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "Loading services..." }) : services.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "No services found" }) : /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: services.map((service) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
          "div",
          {
            className: "rounded-md border border-border/50 bg-muted/50 p-3 shadow-sm transition-colors",
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-wrap items-center justify-between gap-2", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-w-0 flex-1 items-center gap-2", children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    Badge,
                    {
                      variant: "outline",
                      className: `shrink-0 text-xs ${getServiceStatusColor(service.status)}`,
                      children: getServiceStatusDisplay(service)
                    }
                  ),
                  /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate text-sm font-medium", children: service.name })
                ] }),
                /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex shrink-0 items-center gap-2", children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "text-xs tabular-nums text-muted-foreground", children: [
                    service.ready_replicas,
                    "/",
                    service.total_replicas
                  ] }),
                  service.restarts > 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-xs text-destructive", children: [
                    "· ",
                    service.restarts,
                    " restarts"
                  ] }),
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    "button",
                    {
                      onClick: () => openServiceDetails(service),
                      className: "flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-2 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                      title: "View details",
                      "aria-label": "View service details",
                      children: /* @__PURE__ */ jsxRuntimeExports.jsx(Info, { className: "size-4" })
                    }
                  )
                ] })
              ] }),
              service.endpoint && /* @__PURE__ */ jsxRuntimeExports.jsxs(
                "a",
                {
                  href: `https://${service.endpoint}`,
                  target: "_blank",
                  rel: "noopener noreferrer",
                  className: "mt-2 flex items-center gap-1.5 text-xs text-cyan-500 transition-colors hover:text-cyan-400",
                  children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(ExternalLink, { className: "size-3" }),
                    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: service.endpoint })
                  ]
                }
              ),
              service.custom_domain && service.domain_status !== "active" && service.cname_target && /* @__PURE__ */ jsxRuntimeExports.jsx(
                DomainSetupNotice,
                {
                  customDomain: service.custom_domain,
                  cnameTarget: service.cname_target,
                  domainStatus: service.domain_status
                }
              )
            ]
          },
          service.name
        )) })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 pb-1", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-7 items-center justify-center rounded-md bg-purple-500/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(HardDrive, { className: "size-4 text-purple-500" }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Volumes" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "ml-auto flex items-center gap-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Badge, { variant: "outline", className: "text-xs", children: volumes.length }) })
        ] }),
        !hasStatusData ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "Loading volumes..." }) : volumes.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "No volumes found" }) : /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: volumes.map((volume) => /* @__PURE__ */ jsxRuntimeExports.jsx(
          "div",
          {
            className: "rounded-md border border-border/50 bg-muted/50 p-3 shadow-sm transition-colors",
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "min-w-0 flex-1 truncate text-sm font-medium", children: volume.name }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex shrink-0 items-center gap-2", children: [
                volume.size && /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: volume.size }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs capitalize text-muted-foreground", children: volume.storage_type.toLowerCase() })
              ] })
            ] })
          },
          volume.name
        )) })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2 pb-1", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-7 items-center justify-center rounded-md bg-green-500/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Network, { className: "size-4 text-green-500" }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("h4", { className: "text-sm font-semibold", children: "Networks" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "ml-auto flex items-center gap-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Badge, { variant: "outline", className: "text-xs", children: networks.length }) })
        ] }),
        !hasStatusData ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "Loading networks..." }) : networks.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-dashed p-4 text-center text-xs text-muted-foreground", children: "No networks found" }) : /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: networks.map((network) => /* @__PURE__ */ jsxRuntimeExports.jsx(
          "div",
          {
            className: "rounded-md border border-border/50 bg-muted/50 p-3 shadow-sm transition-colors",
            children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "min-w-0 flex-1 truncate text-sm font-medium", children: network.name }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex shrink-0 items-center gap-2", children: [
                network.driver && /* @__PURE__ */ jsxRuntimeExports.jsx(Badge, { variant: "outline", className: "text-xs", children: network.driver }),
                /* @__PURE__ */ jsxRuntimeExports.jsx(
                  Badge,
                  {
                    variant: "outline",
                    className: `text-xs ${network.status.toLowerCase() === "active" ? "border-green-500/30 bg-green-500/10 text-green-500" : ""}`,
                    children: network.status
                  }
                )
              ] })
            ] })
          },
          network.name
        )) })
      ] })
    ] }) }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      ServiceDetailsSheet,
      {
        service: selectedService,
        open: sheetOpen,
        onOpenChange: setSheetOpen
      }
    )
  ] });
}
const DEPLOYMENTS_HEADER = {
  title: "Deployments",
  description: "View deployments, services, and volumes in this workspace",
  titleSize: "xl"
};
const STATUS_POLL_INTERVAL_MS = 5e3;
function WorkspaceOverview({
  deployments,
  isLoading,
  onDeploymentsChange
}) {
  const [openAccordionValue, setOpenAccordionValue] = reactExports.useState(void 0);
  reactExports.useEffect(() => {
    setOpenAccordionValue(void 0);
  }, [deployments]);
  reactExports.useEffect(() => {
    if (!openAccordionValue) return;
    const pollStatus = async () => {
      try {
        const statusResponse = await getDeploymentStatus({
          data: { deploymentId: openAccordionValue }
        });
        onDeploymentsChange?.(
          deployments.map(
            (d) => d.id === openAccordionValue ? { ...d, status: statusResponse.status, isLoading: false } : d
          )
        );
      } catch (error) {
        console.error(
          `Failed to poll status for deployment ${openAccordionValue}:`,
          error
        );
      }
    };
    void pollStatus();
    const intervalId = setInterval(pollStatus, STATUS_POLL_INTERVAL_MS);
    return () => clearInterval(intervalId);
  }, [openAccordionValue, deployments, onDeploymentsChange]);
  const handleAccordionChange = (value) => {
    setOpenAccordionValue(value);
    if (value) {
      const deployment = deployments.find((d) => d.id === value);
      if (!deployment?.status && !deployment?.isLoading) {
        onDeploymentsChange?.(
          deployments.map(
            (d) => d.id === value ? { ...d, isLoading: true } : d
          )
        );
      }
    }
  };
  if (isLoading) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx(DeploymentsSkeleton, {});
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCard, { className: "border-l-2 border-l-lazycloud/40 shadow-md", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsx(SectionHeader, { ...DEPLOYMENTS_HEADER }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { children: deployments.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col items-center justify-center py-8 text-center sm:py-12", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mb-4 flex size-12 items-center justify-center rounded-lg bg-lazycloud/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Rocket, { className: "size-6 text-lazycloud" }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "mb-2 text-lg font-semibold", children: "No deployments yet" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-md text-sm text-muted-foreground", children: "Deploy your first application to see it appear here" })
    ] }) : /* @__PURE__ */ jsxRuntimeExports.jsx(
      Accordion,
      {
        type: "single",
        collapsible: true,
        className: "w-full space-y-2",
        value: openAccordionValue,
        onValueChange: handleAccordionChange,
        children: deployments.map((deployment) => /* @__PURE__ */ jsxRuntimeExports.jsx(DeploymentCard, { deployment }, deployment.id))
      }
    ) })
  ] });
}
const getRoleColor = (role) => {
  switch (role) {
    case WorkspaceRoles.OWNER:
      return "bg-purple-500/10 text-purple-500 border-purple-500/30";
    case WorkspaceRoles.ADMIN:
      return "bg-blue-500/10 text-blue-500 border-blue-500/30";
    case WorkspaceRoles.MEMBER:
      return "bg-green-500/10 text-green-500 border-green-500/30";
    default:
      return "";
  }
};
function MemberCard({
  member,
  currentUserRole,
  currentUserId,
  workspaceId,
  onUpdate,
  isLoading = false
}) {
  const [isUpdating, setIsUpdating] = reactExports.useState(false);
  const canManage = canManageMembers(currentUserRole);
  const isOwner = member.role === WorkspaceRoles.OWNER;
  const isCurrentUser = member.user_id === currentUserId;
  const isInvited = member.status === "invited";
  const canRemove = canManage && !isOwner && !isCurrentUser;
  const canChangeRole = canManage && !isOwner && !isCurrentUser && !isInvited;
  const handleRoleChange = async (newRole) => {
    if (newRole === member.role || !member.user_id) return;
    setIsUpdating(true);
    try {
      await updateMemberRole({
        data: {
          workspaceId,
          memberUserId: member.user_id,
          role: newRole
        }
      });
      onUpdate?.();
    } catch (error) {
      console.error("Failed to update member role:", error);
    } finally {
      setIsUpdating(false);
    }
  };
  const handleRemove = async () => {
    try {
      if (member.user_id) {
        await removeMember({
          data: { workspaceId, memberUserId: member.user_id }
        });
      }
      onUpdate?.();
    } catch (error) {
      console.error("Failed to remove member:", error);
    }
  };
  if (isLoading) {
    return /* @__PURE__ */ jsxRuntimeExports.jsx(
      StyledCard,
      {
        variant: "minimal",
        className: "gap-0 overflow-hidden border-muted p-4",
        children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between gap-3", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-5 w-64" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx(Skeleton, { className: "h-8 w-24" })
        ] })
      }
    );
  }
  const hasName = member.name?.trim();
  return /* @__PURE__ */ jsxRuntimeExports.jsx(
    StyledCard,
    {
      variant: "minimal",
      className: "gap-0 overflow-hidden border-border/50 bg-muted/30 p-4 shadow-sm",
      children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-h-[32px] items-center justify-between gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex min-w-0 flex-1 items-center gap-1.5", children: [
          member.role === WorkspaceRoles.OWNER && /* @__PURE__ */ jsxRuntimeExports.jsx(Shield, { className: "size-4 shrink-0 text-purple-500" }),
          member.role === WorkspaceRoles.ADMIN && /* @__PURE__ */ jsxRuntimeExports.jsx(User, { className: "size-4 shrink-0 text-blue-500" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "truncate text-sm", children: hasName ? /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium", children: member.name }),
            /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "text-muted-foreground", children: [
              " · ",
              member.email
            ] })
          ] }) : /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium", children: member.email }) })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex shrink-0 items-center gap-2", children: [
          isCurrentUser && /* @__PURE__ */ jsxRuntimeExports.jsx(Badge, { variant: "outline", className: "shrink-0 text-xs", children: "You" }),
          canChangeRole ? /* @__PURE__ */ jsxRuntimeExports.jsxs(
            Select,
            {
              value: member.role,
              onValueChange: (value) => handleRoleChange(value),
              disabled: isUpdating,
              children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx(SelectTrigger, { className: "h-11 w-full cursor-pointer text-xs sm:w-[120px]", children: /* @__PURE__ */ jsxRuntimeExports.jsx(SelectValue, {}) }),
                /* @__PURE__ */ jsxRuntimeExports.jsxs(SelectContent, { children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    SelectItem,
                    {
                      value: WorkspaceRoles.MEMBER,
                      className: "cursor-pointer",
                      children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
                        /* @__PURE__ */ jsxRuntimeExports.jsx(User, { className: "size-3" }),
                        "Member"
                      ] })
                    }
                  ),
                  /* @__PURE__ */ jsxRuntimeExports.jsx(
                    SelectItem,
                    {
                      value: WorkspaceRoles.ADMIN,
                      className: "cursor-pointer",
                      children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
                        /* @__PURE__ */ jsxRuntimeExports.jsx(Shield, { className: "size-3" }),
                        "Admin"
                      ] })
                    }
                  )
                ] })
              ]
            }
          ) : /* @__PURE__ */ jsxRuntimeExports.jsx(
            Badge,
            {
              variant: "outline",
              className: `text-xs capitalize ${getRoleColor(member.role)}`,
              children: member.role
            }
          ),
          canRemove && /* @__PURE__ */ jsxRuntimeExports.jsx(StyledTooltip, { content: "Remove member", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            ExpandConfirmButton,
            {
              onConfirm: handleRemove,
              icon: Trash2,
              color: "red",
              buttonClassName: "text-red-500 hover:bg-red-500/10 cursor-pointer"
            }
          ) }) })
        ] })
      ] })
    }
  );
}
function InviteMembersDialog({
  open,
  onOpenChange,
  workspaces,
  currentWorkspaceId,
  currentUserRole,
  onReload
}) {
  const router = useRouter();
  const [email, setEmail] = reactExports.useState("");
  const [selectedWorkspaceIds, setSelectedWorkspaceIds] = reactExports.useState([]);
  const [role, setRole] = reactExports.useState(WorkspaceRoles.MEMBER);
  const [isInviting, setIsInviting] = reactExports.useState(false);
  const [popoverOpen, setPopoverOpen] = reactExports.useState(false);
  const [errors, setErrors] = reactExports.useState({});
  const nonPersonalWorkspaces = workspaces.filter((w) => !w.is_personal);
  const canInvite = canInviteMembers(currentUserRole);
  reactExports.useEffect(() => {
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
  const toggleWorkspace = (workspaceId) => {
    setSelectedWorkspaceIds(
      (prev) => prev.includes(workspaceId) ? prev.filter((id) => id !== workspaceId) : [...prev, workspaceId]
    );
  };
  const handleInvite = async (e) => {
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
    const results = [];
    for (const workspaceId of selectedWorkspaceIds) {
      try {
        await inviteUser({
          data: { workspaceId, email: email.trim(), role }
        });
        results.push({ workspaceId, success: true });
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : "Failed to invite user";
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
        general: `Failed to invite to: ${failedWorkspaces.join(", ")}`
      });
      toast.error(`Failed to invite to: ${failedWorkspaces.join(", ")}`);
    }
    if (successful > 0) {
      const successfulWorkspaces = results.filter((r) => r.success).map((r) => {
        const workspace = workspaces.find((w) => w.id === r.workspaceId);
        return workspace?.name ?? r.workspaceId;
      });
      const message = successfulWorkspaces.length === 1 ? `Invitation sent to ${email.trim()} for ${successfulWorkspaces[0]}` : `Invitations sent to ${email.trim()} for ${successfulWorkspaces.length} workspace${successfulWorkspaces.length > 1 ? "s" : ""}`;
      toast.success(message);
      setEmail("");
      setSelectedWorkspaceIds(currentWorkspaceId ? [currentWorkspaceId] : []);
      setRole(WorkspaceRoles.MEMBER);
      onOpenChange(false);
      if (onReload) {
        onReload();
      } else {
        router.invalidate();
      }
    }
    setIsInviting(false);
  };
  const handleCancel = () => {
    onOpenChange(false);
    setEmail("");
    setSelectedWorkspaceIds(currentWorkspaceId ? [currentWorkspaceId] : []);
    setRole(WorkspaceRoles.MEMBER);
    setErrors({});
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Dialog, { open, onOpenChange, children: /* @__PURE__ */ jsxRuntimeExports.jsx(DialogContent, { className: "sm:max-w-[500px] [&>button[data-slot='dialog-close']]:cursor-pointer", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("form", { onSubmit: handleInvite, children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogTitle, { children: "Invite Members" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogDescription, { children: "Invite users to workspaces by email" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-4 py-4", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Label, { htmlFor: "email", children: "Email" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          Input,
          {
            id: "email",
            type: "email",
            placeholder: "user@example.com",
            value: email,
            onChange: (e) => {
              setEmail(e.target.value);
              setErrors((prev) => ({ ...prev, email: "" }));
            },
            disabled: isInviting,
            autoFocus: true
          }
        ),
        errors.email && /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-red-500", children: errors.email })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Label, { children: "Workspaces" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs(Popover, { open: popoverOpen, onOpenChange: setPopoverOpen, children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(PopoverTrigger, { asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
            Button,
            {
              type: "button",
              variant: "outline",
              role: "combobox",
              "aria-expanded": popoverOpen,
              className: "w-full cursor-pointer justify-between",
              disabled: isInviting,
              children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "truncate", children: selectedWorkspaceIds.length === 0 ? "Select workspaces..." : selectedWorkspaceIds.length === 1 ? nonPersonalWorkspaces.find(
                  (w) => w.id === selectedWorkspaceIds[0]
                )?.name : `${selectedWorkspaceIds.length} workspaces selected` }),
                /* @__PURE__ */ jsxRuntimeExports.jsx(ChevronsUpDown, { className: "ml-2 size-4 shrink-0 opacity-50" })
              ]
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx(PopoverContent, { className: "w-[--radix-popover-trigger-width] p-0", children: /* @__PURE__ */ jsxRuntimeExports.jsxs(Command, { children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(
              CommandInput,
              {
                placeholder: "Search workspaces...",
                className: "h-9"
              }
            ),
            /* @__PURE__ */ jsxRuntimeExports.jsxs(CommandList, { children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(CommandEmpty, { children: "No workspace found." }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(CommandGroup, { children: nonPersonalWorkspaces.map((workspace) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
                CommandItem,
                {
                  value: workspace.name,
                  onSelect: () => toggleWorkspace(workspace.id),
                  className: "cursor-pointer",
                  children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(
                      Check,
                      {
                        className: cn(
                          "mr-2 size-4",
                          selectedWorkspaceIds.includes(workspace.id) ? "opacity-100" : "opacity-0"
                        )
                      }
                    ),
                    workspace.name
                  ]
                },
                workspace.id
              )) })
            ] })
          ] }) })
        ] }),
        errors.workspaces && /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-red-500", children: errors.workspaces })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-3", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Label, { children: "Role" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs(
          RadioGroup,
          {
            value: role,
            onValueChange: (value) => setRole(value),
            disabled: isInviting,
            className: "flex flex-col gap-3 sm:flex-row sm:gap-6",
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center space-x-2", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx(
                  RadioGroupItem,
                  {
                    value: WorkspaceRoles.MEMBER,
                    id: "role-member",
                    className: "cursor-pointer"
                  }
                ),
                /* @__PURE__ */ jsxRuntimeExports.jsxs(
                  Label,
                  {
                    htmlFor: "role-member",
                    className: "flex cursor-pointer items-center gap-1.5 font-normal",
                    children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx(User, { className: "h-3.5 w-3.5" }),
                      "Member"
                    ]
                  }
                )
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center space-x-2", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx(
                  RadioGroupItem,
                  {
                    value: WorkspaceRoles.ADMIN,
                    id: "role-admin",
                    className: "cursor-pointer"
                  }
                ),
                /* @__PURE__ */ jsxRuntimeExports.jsxs(
                  Label,
                  {
                    htmlFor: "role-admin",
                    className: "flex cursor-pointer items-center gap-1.5 font-normal",
                    children: [
                      /* @__PURE__ */ jsxRuntimeExports.jsx(Shield, { className: "h-3.5 w-3.5" }),
                      "Admin"
                    ]
                  }
                )
              ] })
            ]
          }
        ),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-blue-500/30 bg-blue-500/5 px-3 py-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-xs text-muted-foreground", children: role === WorkspaceRoles.ADMIN ? /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium text-blue-600 dark:text-blue-400", children: "Admins" }),
          " ",
          "can create/manage deployments and invite members"
        ] }) : /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-medium text-blue-600 dark:text-blue-400", children: "Members" }),
          " ",
          "can view deployments and monitor services"
        ] }) }) })
      ] }),
      errors.general && /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-red-500/30 bg-red-500/10 p-3", children: /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm text-red-500", children: errors.general }) }),
      !canInvite && /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-center text-sm text-muted-foreground", children: "You need owner or admin permissions to invite members" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogFooter, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "outline",
          onClick: handleCancel,
          disabled: isInviting,
          className: "cursor-pointer",
          children: "Cancel"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsxs(
        Button,
        {
          type: "submit",
          disabled: isInviting || !email.trim() || selectedWorkspaceIds.length === 0 || !canInvite,
          className: "cursor-pointer",
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(UserPlus, { className: "mr-2 size-4" }),
            isInviting ? "Inviting..." : "Invite"
          ]
        }
      )
    ] })
  ] }) }) });
}
function TransferOwnershipDialog({
  open,
  onOpenChange,
  members,
  workspaceId,
  workspaceName,
  pendingTransfer: pendingTransferProp,
  onTransferChange
}) {
  const router = useRouter();
  const [selectedAdminUserId, setSelectedAdminUserId] = reactExports.useState("");
  const [isTransferring, setIsTransferring] = reactExports.useState(false);
  const [isCancelling, setIsCancelling] = reactExports.useState(false);
  const [pendingTransfer, setPendingTransfer] = reactExports.useState(pendingTransferProp ?? null);
  const adminMembers = members.filter(
    (member) => member.role === WorkspaceRoles.ADMIN && member.status === "active" && member.user_id !== null && member.email
  );
  reactExports.useEffect(() => {
    setPendingTransfer(pendingTransferProp ?? null);
  }, [pendingTransferProp]);
  reactExports.useEffect(() => {
    if (!open) {
      setSelectedAdminUserId("");
    }
  }, [open]);
  const handleCancelPendingTransfer = async () => {
    if (!pendingTransfer?.invitation_id) {
      toast.error("Invitation ID not available");
      return;
    }
    setIsCancelling(true);
    const toastId = toast.loading("Cancelling ownership transfer invitation...");
    try {
      await cancelInvitation({
        data: {
          workspaceId,
          invitationId: pendingTransfer.invitation_id
        }
      });
      toast.success("Ownership transfer invitation cancelled.", { id: toastId });
      setPendingTransfer(null);
      onTransferChange?.();
      router.invalidate();
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Failed to cancel invitation. Please try again.",
        { id: toastId }
      );
    } finally {
      setIsCancelling(false);
    }
  };
  const handleTransfer = async () => {
    if (!selectedAdminUserId) {
      toast.error("Please select an admin to transfer ownership to");
      return;
    }
    setIsTransferring(true);
    const toastId = toast.loading("Sending ownership transfer invitation...");
    try {
      await transferOwnership({
        data: {
          workspaceId,
          newOwnerUserId: selectedAdminUserId
        }
      });
      toast.success(
        "Ownership transfer invitation sent successfully. The admin will receive an email to accept the transfer.",
        { id: toastId }
      );
      onOpenChange(false);
      onTransferChange?.();
      setTimeout(() => {
        setSelectedAdminUserId("");
        router.invalidate();
      }, 100);
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Failed to send ownership transfer invitation. Please try again.",
        { id: toastId }
      );
    } finally {
      setIsTransferring(false);
    }
  };
  const handleCancel = () => {
    onOpenChange(false);
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Dialog, { open, onOpenChange: handleCancel, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogContent, { className: "sm:max-w-[500px] [&>button[data-slot='dialog-close']]:cursor-pointer", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogTitle, { children: "Transfer Workspace Ownership" }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogDescription, { children: [
        "Transfer ownership of ",
        /* @__PURE__ */ jsxRuntimeExports.jsx("strong", { children: workspaceName }),
        " to an admin. You will become an admin after the transfer is accepted."
      ] })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "grid gap-4 py-4", children: pendingTransfer ? /* @__PURE__ */ jsxRuntimeExports.jsxs(
      StyledCard,
      {
        variant: "minimal",
        className: "gap-0 overflow-hidden border-purple-500/30 bg-purple-500/10 py-2 shadow-sm",
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(Crown, { className: "size-4 text-purple-500" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "truncate font-medium", children: pendingTransfer.name ?? pendingTransfer.email }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Badge,
                {
                  variant: "outline",
                  className: "border-purple-500/30 bg-purple-500/10 text-xs text-purple-500",
                  children: "Pending Transfer"
                }
              )
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsx(
              Button,
              {
                variant: "ghost",
                size: "sm",
                onClick: handleCancelPendingTransfer,
                disabled: isCancelling,
                className: "size-11 shrink-0 p-0 text-red-500 hover:bg-red-500/10",
                "aria-label": "Cancel transfer",
                children: isCancelling ? /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "size-4 animate-spin text-red-500" }) : /* @__PURE__ */ jsxRuntimeExports.jsx(Trash2, { className: "size-4" })
              }
            )
          ] }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCardContent, { className: "p-0", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(Separator, {}),
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "px-4 py-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "text-xs text-muted-foreground", children: pendingTransfer.email }),
              /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mt-1 text-xs text-muted-foreground", children: "An ownership transfer invitation has been sent. You cannot send another until this one is accepted or cancelled." })
            ] })
          ] })
        ]
      }
    ) : adminMembers.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-yellow-500/30 bg-yellow-500/10 p-4", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-start gap-3", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(TriangleAlert, { className: "mt-0.5 size-5 text-yellow-600" }),
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "text-sm font-medium text-yellow-800", children: "No admins available" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "mt-1 text-sm text-yellow-700", children: "You need at least one admin member before you can transfer ownership. Please promote a member to admin first." })
      ] })
    ] }) }) : /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "grid gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("label", { className: "text-sm font-medium", children: "Select Admin to Transfer To" }),
        /* @__PURE__ */ jsxRuntimeExports.jsxs(
          Select,
          {
            value: selectedAdminUserId,
            onValueChange: setSelectedAdminUserId,
            disabled: isTransferring,
            children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(SelectTrigger, { className: "cursor-pointer", children: /* @__PURE__ */ jsxRuntimeExports.jsx(SelectValue, { placeholder: "Choose an admin..." }) }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(SelectContent, { children: adminMembers.map((admin) => /* @__PURE__ */ jsxRuntimeExports.jsx(
                SelectItem,
                {
                  value: admin.user_id,
                  className: "cursor-pointer",
                  children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(Shield, { className: "size-4 text-blue-500" }),
                    /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: admin.name ?? admin.email }),
                    admin.email !== admin.name && /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "text-xs text-muted-foreground", children: admin.email })
                  ] })
                },
                admin.user_id
              )) })
            ]
          }
        )
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "rounded-md border border-blue-500/30 bg-blue-500/10 p-4", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("p", { className: "text-sm", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx("strong", { children: "Important:" }),
        " The selected admin will receive an email invitation to accept the ownership transfer. They must have a subscription plan that supports all resources in this workspace."
      ] }) })
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogFooter, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "outline",
          onClick: handleCancel,
          disabled: isTransferring,
          className: "cursor-pointer",
          children: pendingTransfer ? "Close" : "Cancel"
        }
      ),
      !pendingTransfer && /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          type: "button",
          variant: "secondary",
          onClick: handleTransfer,
          disabled: isTransferring || adminMembers.length === 0 || !selectedAdminUserId,
          className: "cursor-pointer bg-lazycloud hover:bg-lazycloud/80 active:bg-lazycloud/70",
          children: isTransferring ? /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "animate-spin" }) : "Send Transfer Invitation"
        }
      )
    ] })
  ] }) });
}
function PendingInvitationsDialog({
  open,
  onOpenChange,
  workspaceId,
  invitations: initialInvitations,
  onReload
}) {
  const [invitations, setInvitations] = reactExports.useState(initialInvitations);
  const [deletingEmail, setDeletingEmail] = reactExports.useState(null);
  const [resendingEmail, setResendingEmail] = reactExports.useState(null);
  reactExports.useEffect(() => {
    setInvitations(initialInvitations);
  }, [initialInvitations]);
  const handleDelete = async (invitation) => {
    const invitationId = invitation.invitation_id;
    if (!invitationId || typeof invitationId === "string" && invitationId.trim() === "") {
      toast.error(`Invitation ID not available for ${invitation.email}`);
      return;
    }
    setDeletingEmail(invitation.email);
    const toastId = toast.loading("Cancelling invitation...");
    try {
      await cancelInvitation({
        data: { workspaceId, invitationId }
      });
      toast.success("Invitation cancelled successfully.", { id: toastId });
      setInvitations(
        (prev) => prev.filter((inv) => inv.invitation_id !== invitation.invitation_id)
      );
      onReload?.();
    } catch (error) {
      console.error("Failed to cancel invitation:", error);
      const errorMessage = error instanceof Error ? error.message : "Failed to cancel invitation. Please try again.";
      toast.error(errorMessage, { id: toastId });
      onReload?.();
    } finally {
      setDeletingEmail(null);
    }
  };
  const handleResend = async (email, role) => {
    setResendingEmail(email);
    const toastId = toast.loading("Resending invitation...");
    try {
      await inviteUser({
        data: { workspaceId, email, role }
      });
      toast.success("Invitation resent successfully.", { id: toastId });
      onReload?.();
    } catch (error) {
      console.error("Failed to resend invitation:", error);
      const errorMessage = error instanceof Error ? error.message : "Failed to resend invitation. Please try again.";
      toast.error(errorMessage, { id: toastId });
    } finally {
      setResendingEmail(null);
    }
  };
  const getRoleColor2 = (role) => {
    switch (role) {
      case WorkspaceRoles.OWNER:
        return "bg-purple-500/10 text-purple-500 border-purple-500/30";
      case WorkspaceRoles.ADMIN:
        return "bg-blue-500/10 text-blue-500 border-blue-500/30";
      case WorkspaceRoles.MEMBER:
        return "bg-green-500/10 text-green-500 border-green-500/30";
      default:
        return "";
    }
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsx(Dialog, { open, onOpenChange, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogContent, { className: "max-w-2xl", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogHeader, { children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs(DialogTitle, { className: "flex items-center gap-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(Mail, { className: "size-5" }),
        "Pending Invitations"
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(DialogDescription, { children: "View and manage invitations that have been sent but not yet accepted" })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-4", children: invitations.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col items-center justify-center py-12 text-center", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mb-4 flex size-12 items-center justify-center rounded-lg bg-muted", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Mail, { className: "size-6 text-muted-foreground" }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "mb-2 text-lg font-semibold", children: "No pending invitations" }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-md text-sm text-muted-foreground", children: "All invitations have been accepted or there are no pending invitations for this workspace" })
    ] }) : /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: invitations.map((invitation) => /* @__PURE__ */ jsxRuntimeExports.jsxs(
      StyledCard,
      {
        variant: "minimal",
        className: "gap-0 overflow-hidden border-border/50 bg-muted/30 py-2 shadow-sm",
        children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "truncate font-medium", children: invitation.name ?? invitation.email }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Badge,
                {
                  variant: "outline",
                  className: `text-xs capitalize ${getRoleColor2(invitation.role)}`,
                  children: invitation.role
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Badge,
                {
                  variant: "outline",
                  className: "border-yellow-500/30 bg-yellow-500/10 text-xs text-yellow-500",
                  children: "Pending"
                }
              )
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(StyledTooltip, { content: "Resend invitation", children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                Button,
                {
                  variant: "outline",
                  size: "sm",
                  onClick: () => handleResend(invitation.email, invitation.role),
                  disabled: resendingEmail === invitation.email,
                  className: "size-11 shrink-0",
                  "aria-label": "Resend invitation",
                  children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                    Send,
                    {
                      className: `size-4 ${resendingEmail === invitation.email ? "animate-spin" : ""}`
                    }
                  )
                }
              ) }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(StyledTooltip, { content: "Cancel invitation", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { children: /* @__PURE__ */ jsxRuntimeExports.jsx(
                ExpandConfirmButton,
                {
                  onConfirm: () => handleDelete(invitation),
                  icon: Trash2,
                  color: "red",
                  buttonClassName: `text-red-500 hover:bg-red-500/10 cursor-pointer ${deletingEmail === invitation.email ? "opacity-50 cursor-not-allowed" : ""}`
                }
              ) }) })
            ] })
          ] }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCardContent, { className: "p-0", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(Separator, {}),
            /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "px-4 py-2", children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "text-xs text-muted-foreground", children: invitation.email }) })
          ] })
        ]
      },
      invitation.user_id ?? invitation.email
    )) }) })
  ] }) });
}
function MemberList({
  members: initialMembers,
  currentUserRole,
  currentUserId,
  workspaceId,
  workspaces = [],
  isPersonalWorkspace = false,
  isLoading = false,
  onReload
}) {
  const router = useRouter();
  const [members, setMembers] = reactExports.useState(initialMembers);
  const [searchQuery, setSearchQuery] = reactExports.useState("");
  const [roleFilter, setRoleFilter] = reactExports.useState("all");
  const [statusFilter, setStatusFilter] = reactExports.useState("all");
  const [inviteDialogOpen, setInviteDialogOpen] = reactExports.useState(false);
  const [transferDialogOpen, setTransferDialogOpen] = reactExports.useState(false);
  const [pendingInvitationsDialogOpen, setPendingInvitationsDialogOpen] = reactExports.useState(false);
  const [hasPendingOwnershipTransfer, setHasPendingOwnershipTransfer] = reactExports.useState(false);
  const [pendingOwnershipTransfer, setPendingOwnershipTransfer] = reactExports.useState(null);
  reactExports.useEffect(() => {
    const activeMembers = initialMembers.filter(
      (m) => m.status.toLowerCase() !== "invited"
    );
    setMembers(activeMembers);
  }, [initialMembers]);
  reactExports.useEffect(() => {
    if (currentUserRole === WorkspaceRoles.OWNER && !isPersonalWorkspace && workspaceId) {
      const checkPendingTransfer = async () => {
        try {
          const pending = await getPendingOwnershipTransfer({
            data: { workspaceId }
          });
          setHasPendingOwnershipTransfer(!!pending);
          setPendingOwnershipTransfer(pending);
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : String(error);
          const lowerErrorMessage = errorMessage.toLowerCase();
          if (!lowerErrorMessage.includes("owner role required") && !lowerErrorMessage.includes("403") && !lowerErrorMessage.includes("forbidden")) {
            console.error("Failed to check pending ownership transfer:", error);
          }
          setHasPendingOwnershipTransfer(false);
          setPendingOwnershipTransfer(null);
        }
      };
      void checkPendingTransfer();
    } else {
      setHasPendingOwnershipTransfer(false);
      setPendingOwnershipTransfer(null);
    }
  }, [currentUserRole, isPersonalWorkspace, workspaceId]);
  const handleUpdate = async () => {
    if (onReload) {
      onReload();
    } else {
      router.invalidate();
    }
    if (currentUserRole === WorkspaceRoles.OWNER && !isPersonalWorkspace && workspaceId) {
      try {
        const pending = await getPendingOwnershipTransfer({
          data: { workspaceId }
        });
        setHasPendingOwnershipTransfer(!!pending);
        setPendingOwnershipTransfer(pending);
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : String(error);
        const lowerErrorMessage = errorMessage.toLowerCase();
        if (!lowerErrorMessage.includes("owner role required") && !lowerErrorMessage.includes("403") && !lowerErrorMessage.includes("forbidden")) {
          console.error("Failed to check pending ownership transfer:", error);
        }
        setHasPendingOwnershipTransfer(false);
        setPendingOwnershipTransfer(null);
      }
    }
  };
  const filteredMembers = reactExports.useMemo(() => {
    return members.filter((member) => {
      const matchesSearch = searchQuery === "" || (member.name?.toLowerCase().includes(searchQuery.toLowerCase()) ?? false) || member.email.toLowerCase().includes(searchQuery.toLowerCase());
      const matchesRole = roleFilter === "all" || member.role === roleFilter;
      const matchesStatus = statusFilter === "all" || member.status.toLowerCase() === statusFilter.toLowerCase();
      return matchesSearch && matchesRole && matchesStatus;
    });
  }, [members, searchQuery, roleFilter, statusFilter]);
  const pendingInvitations = reactExports.useMemo(() => {
    return initialMembers.filter((m) => m.status.toLowerCase() === "invited");
  }, [initialMembers]);
  const pendingInvitationsCount = pendingInvitations.length;
  const sortedMembers = reactExports.useMemo(() => {
    return [...filteredMembers].sort((a, b) => {
      const roleOrder = {
        [WorkspaceRoles.OWNER]: 0,
        [WorkspaceRoles.ADMIN]: 1,
        [WorkspaceRoles.MEMBER]: 2
      };
      const roleDiff = (roleOrder[a.role] ?? 99) - (roleOrder[b.role] ?? 99);
      if (roleDiff !== 0) return roleDiff;
      return (a.name ?? a.email).localeCompare(b.name ?? b.email);
    });
  }, [filteredMembers]);
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(SectionDivider, { spacing: "lg", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCard, { className: "border-l-2 border-l-lazycloud/40 shadow-md", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsx(
          SectionHeader,
          {
            title: "Members",
            description: "Manage workspace members, invitations, and their roles",
            titleSize: "xl"
          }
        ),
        /* @__PURE__ */ jsxRuntimeExports.jsxs(DropdownMenu, { children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx(DropdownMenuTrigger, { asChild: true, children: /* @__PURE__ */ jsxRuntimeExports.jsxs(
            Button,
            {
              variant: "outline",
              size: "sm",
              className: "relative h-9 shrink-0",
              children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx(EllipsisVertical, { className: "size-4" }),
                (pendingInvitationsCount > 0 || hasPendingOwnershipTransfer) && /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "absolute -right-1 -top-1 flex size-5 items-center justify-center rounded-full bg-blue-500 text-[10px] font-bold text-white", children: pendingInvitationsCount + (hasPendingOwnershipTransfer ? 1 : 0) })
              ]
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs(DropdownMenuContent, { align: "end", children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs(
              DropdownMenuItem,
              {
                onClick: () => setInviteDialogOpen(true),
                disabled: isPersonalWorkspace,
                className: "cursor-pointer",
                children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx(UserPlus, { className: "mr-2 size-4" }),
                  "Invite Members"
                ]
              }
            ),
            /* @__PURE__ */ jsxRuntimeExports.jsxs(
              DropdownMenuItem,
              {
                onClick: () => setPendingInvitationsDialogOpen(true),
                className: "cursor-pointer",
                children: [
                  /* @__PURE__ */ jsxRuntimeExports.jsx(Mail, { className: "mr-2 size-4" }),
                  "View Pending Invitations",
                  pendingInvitationsCount > 0 && /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { className: "ml-auto text-xs text-muted-foreground", children: [
                    "(",
                    pendingInvitationsCount,
                    ")"
                  ] })
                ]
              }
            ),
            currentUserRole === WorkspaceRoles.OWNER && !isPersonalWorkspace && /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(DropdownMenuSeparator, {}),
              /* @__PURE__ */ jsxRuntimeExports.jsxs(
                DropdownMenuItem,
                {
                  onClick: () => setTransferDialogOpen(true),
                  className: "cursor-pointer",
                  children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(Crown, { className: "mr-2 size-4" }),
                    "Transfer Ownership",
                    hasPendingOwnershipTransfer && /* @__PURE__ */ jsxRuntimeExports.jsx(
                      Badge,
                      {
                        variant: "outline",
                        className: "ml-auto border-purple-500/30 bg-purple-500/10 text-xs text-purple-500",
                        children: "Pending"
                      }
                    )
                  ]
                }
              )
            ] })
          ] })
        ] })
      ] }) }),
      /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-4", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col gap-3 sm:flex-row", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex-1", children: /* @__PURE__ */ jsxRuntimeExports.jsx(
            Input,
            {
              placeholder: "Search by name or email...",
              value: searchQuery,
              onChange: (e) => setSearchQuery(e.target.value),
              className: "w-full"
            }
          ) }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs(Select, { value: roleFilter, onValueChange: setRoleFilter, children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(SelectTrigger, { className: "w-full cursor-pointer sm:w-[180px]", children: /* @__PURE__ */ jsxRuntimeExports.jsx(SelectValue, { placeholder: "Filter by role" }) }),
            /* @__PURE__ */ jsxRuntimeExports.jsxs(SelectContent, { children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(SelectItem, { value: "all", className: "cursor-pointer", children: "All Roles" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                SelectItem,
                {
                  value: WorkspaceRoles.OWNER,
                  className: "cursor-pointer",
                  children: "Owner"
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                SelectItem,
                {
                  value: WorkspaceRoles.ADMIN,
                  className: "cursor-pointer",
                  children: "Admin"
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                SelectItem,
                {
                  value: WorkspaceRoles.MEMBER,
                  className: "cursor-pointer",
                  children: "Member"
                }
              )
            ] })
          ] }),
          /* @__PURE__ */ jsxRuntimeExports.jsxs(Select, { value: statusFilter, onValueChange: setStatusFilter, children: [
            /* @__PURE__ */ jsxRuntimeExports.jsx(SelectTrigger, { className: "w-full cursor-pointer sm:w-[180px]", children: /* @__PURE__ */ jsxRuntimeExports.jsx(SelectValue, { placeholder: "Filter by status" }) }),
            /* @__PURE__ */ jsxRuntimeExports.jsxs(SelectContent, { children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(SelectItem, { value: "all", className: "cursor-pointer", children: "All Status" }),
              /* @__PURE__ */ jsxRuntimeExports.jsx(SelectItem, { value: "active", className: "cursor-pointer", children: "Active" })
            ] })
          ] })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-2", children: isLoading && members.length === 0 ? null : sortedMembers.length === 0 ? /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col items-center justify-center py-12 text-center", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mb-4 flex size-12 items-center justify-center rounded-lg bg-muted", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Users, { className: "size-6 text-muted-foreground" }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("h3", { className: "mb-2 text-lg font-semibold", children: members.length === 0 ? "No members yet" : "No members match your filters" }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "max-w-md text-sm text-muted-foreground", children: members.length === 0 ? "Invite team members to collaborate on this workspace" : "Try adjusting your search or filter criteria" })
        ] }) : sortedMembers.map((member) => /* @__PURE__ */ jsxRuntimeExports.jsx(
          MemberCard,
          {
            member,
            currentUserRole,
            currentUserId,
            workspaceId,
            onUpdate: handleUpdate
          },
          member.user_id ?? member.email
        )) })
      ] }) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      InviteMembersDialog,
      {
        open: inviteDialogOpen,
        onOpenChange: setInviteDialogOpen,
        workspaces,
        currentWorkspaceId: workspaceId,
        currentUserRole,
        onReload: handleUpdate
      }
    ),
    /* @__PURE__ */ jsxRuntimeExports.jsx(
      PendingInvitationsDialog,
      {
        open: pendingInvitationsDialogOpen,
        onOpenChange: setPendingInvitationsDialogOpen,
        workspaceId,
        invitations: pendingInvitations,
        onReload: handleUpdate
      }
    ),
    workspaces.length > 0 && !isPersonalWorkspace && /* @__PURE__ */ jsxRuntimeExports.jsx(
      TransferOwnershipDialog,
      {
        open: transferDialogOpen,
        onOpenChange: (open) => {
          setTransferDialogOpen(open);
          if (!open && currentUserRole === WorkspaceRoles.OWNER) {
            const checkPendingTransfer = async () => {
              try {
                const pending = await getPendingOwnershipTransfer({
                  data: { workspaceId }
                });
                setHasPendingOwnershipTransfer(!!pending);
                setPendingOwnershipTransfer(pending);
              } catch (error) {
                const errorMessage = error instanceof Error ? error.message : String(error);
                if (!errorMessage.includes("Owner role required") && !errorMessage.includes("403")) {
                  console.error(
                    "Failed to check pending ownership transfer:",
                    error
                  );
                }
                setHasPendingOwnershipTransfer(false);
                setPendingOwnershipTransfer(null);
              }
            };
            void checkPendingTransfer();
          }
        },
        members,
        workspaceId,
        workspaceName: workspaces.find((w) => w.id === workspaceId)?.name ?? "Workspace",
        pendingTransfer: pendingOwnershipTransfer,
        onTransferChange: handleUpdate
      }
    )
  ] });
}
function MemberListWrapper({
  workspaceId,
  workspaces = []
}) {
  const [members, setMembers] = reactExports.useState([]);
  const [currentUserRole, setCurrentUserRole] = reactExports.useState();
  const [currentUserId, setCurrentUserId] = reactExports.useState();
  const [isLoading, setIsLoading] = reactExports.useState(true);
  const loadMembers = async () => {
    if (!workspaceId) {
      setIsLoading(true);
      setMembers([]);
      setCurrentUserRole(void 0);
      setCurrentUserId(void 0);
      return;
    }
    setIsLoading(true);
    try {
      const [currentUserInternalId, membersData] = await Promise.all([
        getCurrentUserInternalId(),
        getWorkspaceMembers({ data: { workspaceId } })
      ]);
      const currentUserMember = membersData.find(
        (m) => m.user_id === currentUserInternalId
      );
      setCurrentUserId(currentUserInternalId);
      setMembers(membersData);
      setCurrentUserRole(currentUserMember?.role);
    } catch (error) {
      console.error("Failed to load workspace members:", error);
    } finally {
      setIsLoading(false);
    }
  };
  reactExports.useEffect(() => {
    let cancelled = false;
    const loadMembersWithCancel = async () => {
      if (!workspaceId) {
        setIsLoading(true);
        setMembers([]);
        setCurrentUserRole(void 0);
        setCurrentUserId(void 0);
        return;
      }
      setIsLoading(true);
      try {
        const [currentUserInternalId, membersData] = await Promise.all([
          getCurrentUserInternalId(),
          getWorkspaceMembers({ data: { workspaceId } })
        ]);
        if (cancelled) return;
        const currentUserMember = membersData.find(
          (m) => m.user_id === currentUserInternalId
        );
        if (cancelled) return;
        setCurrentUserId(currentUserInternalId);
        setMembers(membersData);
        setCurrentUserRole(currentUserMember?.role);
      } catch (error) {
        if (cancelled) return;
        console.error("Failed to load workspace members:", error);
      } finally {
        if (!cancelled) {
          setIsLoading(false);
        }
      }
    };
    void loadMembersWithCancel();
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);
  const currentWorkspace = workspaces.find((w) => w.id === workspaceId);
  return workspaceId ? /* @__PURE__ */ jsxRuntimeExports.jsx(
    MemberList,
    {
      members,
      currentUserRole,
      currentUserId,
      workspaceId,
      workspaces,
      isPersonalWorkspace: currentWorkspace?.is_personal ?? false,
      isLoading,
      onReload: loadMembers
    }
  ) : null;
}
function PendingInvitations() {
  const router = useRouter();
  const [invitations, setInvitations] = reactExports.useState([]);
  const [isLoading, setIsLoading] = reactExports.useState(true);
  const [acceptingId, setAcceptingId] = reactExports.useState(null);
  const [decliningId, setDecliningId] = reactExports.useState(null);
  reactExports.useEffect(() => {
    const loadInvitations = async () => {
      try {
        const data = await getPendingInvitations();
        setInvitations(data ?? []);
      } catch (error) {
        console.error("Failed to load pending invitations:", error);
        setInvitations([]);
      } finally {
        setIsLoading(false);
      }
    };
    void loadInvitations();
  }, []);
  const handleAccept = async (invitationId) => {
    setAcceptingId(invitationId);
    const toastId = toast.loading("Accepting invitation...");
    try {
      await acceptInvitation({
        data: { invitationId }
      });
      toast.success("Invitation accepted successfully!", { id: toastId });
      setInvitations(
        (prev) => prev.filter((inv) => inv.invitation_id !== invitationId)
      );
      router.invalidate();
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Failed to accept invitation",
        { id: toastId }
      );
    } finally {
      setAcceptingId(null);
    }
  };
  const handleDecline = async (invitationId) => {
    setDecliningId(invitationId);
    const toastId = toast.loading("Declining invitation...");
    try {
      await declineInvitation({
        data: { invitationId }
      });
      toast.success("Invitation declined", { id: toastId });
      setInvitations(
        (prev) => prev.filter((inv) => inv.invitation_id !== invitationId)
      );
    } catch (error) {
      toast.error(
        error instanceof Error ? error.message : "Failed to decline invitation",
        { id: toastId }
      );
    } finally {
      setDecliningId(null);
    }
  };
  const handleDismiss = () => {
    setInvitations([]);
  };
  if (isLoading || invitations.length === 0) {
    return null;
  }
  return /* @__PURE__ */ jsxRuntimeExports.jsxs(StyledCard, { className: "border-l-2 border-l-lazycloud/40 shadow-md", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardHeader, { children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        SectionHeader,
        {
          title: "Pending Invitations",
          description: "You have been invited to join these workspaces or accept ownership transfers",
          titleSize: "xl"
        }
      ),
      /* @__PURE__ */ jsxRuntimeExports.jsx(
        Button,
        {
          variant: "ghost",
          size: "sm",
          onClick: handleDismiss,
          className: "size-11 p-0",
          "aria-label": "Dismiss invitations",
          children: /* @__PURE__ */ jsxRuntimeExports.jsx(X, { className: "size-4" })
        }
      )
    ] }) }),
    /* @__PURE__ */ jsxRuntimeExports.jsx(StyledCardContent, { children: /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "space-y-3", children: invitations.map((invitation) => {
      const isOwnershipTransfer = invitation.invitation_type === "ownership_transfer";
      const expiresAt = new Date(invitation.expires_at);
      const isExpired = expiresAt < /* @__PURE__ */ new Date();
      return /* @__PURE__ */ jsxRuntimeExports.jsxs(
        "div",
        {
          className: "flex items-center justify-between rounded-lg border p-3",
          children: [
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex-1", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
                isOwnershipTransfer && /* @__PURE__ */ jsxRuntimeExports.jsx(Crown, { className: "size-4 text-purple-600" }),
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { className: "font-semibold", children: invitation.workspace_name })
              ] }),
              /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "mt-1 text-sm text-muted-foreground", children: [
                /* @__PURE__ */ jsxRuntimeExports.jsx("span", { children: isOwnershipTransfer ? "Ownership transfer" : `Invited as ${invitation.role}` }),
                " • ",
                /* @__PURE__ */ jsxRuntimeExports.jsxs("span", { children: [
                  "by ",
                  invitation.invited_by_name
                ] })
              ] }),
              isExpired && /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "mt-1 text-xs text-destructive", children: "Expired" })
            ] }),
            /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "ml-4 flex shrink-0 gap-2", children: [
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Button,
                {
                  onClick: () => handleDecline(invitation.invitation_id),
                  disabled: isExpired || decliningId === invitation.invitation_id || acceptingId === invitation.invitation_id,
                  variant: "ghost",
                  size: "sm",
                  children: decliningId === invitation.invitation_id ? /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "mr-2 size-4 animate-spin" }),
                    "Declining..."
                  ] }) : /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(CircleX, { className: "mr-2 size-4" }),
                    "Decline"
                  ] })
                }
              ),
              /* @__PURE__ */ jsxRuntimeExports.jsx(
                Button,
                {
                  onClick: () => handleAccept(invitation.invitation_id),
                  disabled: isExpired || acceptingId === invitation.invitation_id || decliningId === invitation.invitation_id,
                  variant: isOwnershipTransfer ? "default" : "outline",
                  size: "sm",
                  children: acceptingId === invitation.invitation_id ? /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(LoaderCircle, { className: "mr-2 size-4 animate-spin" }),
                    "Accepting..."
                  ] }) : /* @__PURE__ */ jsxRuntimeExports.jsxs(jsxRuntimeExports.Fragment, { children: [
                    /* @__PURE__ */ jsxRuntimeExports.jsx(Check, { className: "mr-2 size-4" }),
                    isExpired ? "Expired" : "Accept"
                  ] })
                }
              )
            ] })
          ]
        },
        `${invitation.workspace_id}-${invitation.email}`
      );
    }) }) })
  ] });
}
function WorkspacesPage() {
  const {
    workspaces: initialWorkspaces,
    defaultWorkspaceId,
    initialDeployments
  } = Route$4.useLoaderData();
  const [workspaces, setWorkspaces] = reactExports.useState(initialWorkspaces);
  const [selectedWorkspaceId, setSelectedWorkspaceId] = reactExports.useState(defaultWorkspaceId);
  const [deployments, setDeployments] = reactExports.useState(initialDeployments);
  const [isDeploymentsLoading, setIsDeploymentsLoading] = reactExports.useState(false);
  const handleWorkspaceChange = async (workspaceId) => {
    if (workspaceId === selectedWorkspaceId) return;
    setSelectedWorkspaceId(workspaceId);
    setIsDeploymentsLoading(true);
    setDeployments([]);
    try {
      const workspaceData = await getWorkspaceWithDeployments({
        data: {
          workspaceId
        }
      });
      setDeployments(workspaceData.deployments ?? []);
    } catch (error) {
      console.error("Failed to fetch deployments:", error);
    } finally {
      setIsDeploymentsLoading(false);
    }
  };
  return /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-8", children: [
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-2", children: [
        /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-3", children: [
          /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "flex size-10 items-center justify-center rounded-lg bg-lazycloud/10", children: /* @__PURE__ */ jsxRuntimeExports.jsx(Building2, { className: "size-5 text-lazycloud" }) }),
          /* @__PURE__ */ jsxRuntimeExports.jsx("h1", { className: "text-3xl font-bold tracking-tight", children: "Workspaces" })
        ] }),
        /* @__PURE__ */ jsxRuntimeExports.jsx("p", { className: "ml-[52px] text-sm text-muted-foreground", children: "Manage your workspaces, deployments, and team members" })
      ] }),
      /* @__PURE__ */ jsxRuntimeExports.jsx("div", { className: "sm:pt-1", children: /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "flex items-center gap-2", children: [
        isDeploymentsLoading && /* @__PURE__ */ jsxRuntimeExports.jsx(Spinner, { size: "sm" }),
        /* @__PURE__ */ jsxRuntimeExports.jsx(WorkspaceSelector, { initialWorkspaces: workspaces, currentWorkspaceId: selectedWorkspaceId ?? void 0, onWorkspaceChange: handleWorkspaceChange, onWorkspacesUpdate: setWorkspaces })
      ] }) })
    ] }),
    /* @__PURE__ */ jsxRuntimeExports.jsxs("div", { className: "space-y-6", children: [
      /* @__PURE__ */ jsxRuntimeExports.jsx(PendingInvitations, {}),
      /* @__PURE__ */ jsxRuntimeExports.jsx(WorkspaceOverview, { deployments, isLoading: isDeploymentsLoading, onDeploymentsChange: setDeployments }),
      !workspaces.find((w) => w.id === selectedWorkspaceId)?.is_personal && /* @__PURE__ */ jsxRuntimeExports.jsx(MemberListWrapper, { workspaceId: selectedWorkspaceId, workspaces })
    ] })
  ] });
}
export {
  WorkspacesPage as component
};
