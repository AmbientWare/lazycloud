import { c as createServerRpc } from "./createServerRpc-29xaFZcb.mjs";
import { a as authMiddleware, l as lazycloudApi } from "./auth-CoRbzC04.mjs";
import { e as env } from "./env-vgS3Y8Xp.mjs";
import { c as createServerFn } from "./index.mjs";
import { j as object, k as string, _ as _enum } from "../_libs/zod.mjs";
import "./createMiddleware-CRzJRBrm.mjs";
import "../_chunks/_libs/@t3-oss/env-core.mjs";
import "../_chunks/_libs/@tanstack/history.mjs";
import "../_chunks/_libs/@tanstack/router-core.mjs";
import "../_libs/cookie-es.mjs";
import "../_libs/tiny-invariant.mjs";
import "../_libs/seroval.mjs";
import "../_libs/seroval-plugins.mjs";
import "node:stream/web";
import "node:stream";
import "node:async_hooks";
import "../_libs/h3-v2.mjs";
import "../_libs/rou3.mjs";
import "../_libs/srvx.mjs";
import "../_chunks/_libs/react.mjs";
import "../_chunks/_libs/@tanstack/react-router.mjs";
import "../_libs/tiny-warning.mjs";
import "../_libs/react-dom.mjs";
import "../_libs/isbot.mjs";
const getWorkspaces_createServerFn_handler = createServerRpc({
  id: "31b44d66ad70c0e6ca0b5437193744ce035dfa820ec8c3e4f499b08114ab59e4",
  name: "getWorkspaces",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => getWorkspaces.__executeServer(opts, signal));
const getWorkspaces = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  startDate: string().optional(),
  endDate: string().optional()
}).optional()).handler(getWorkspaces_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getWorkspaces(context.accessToken, data?.startDate, data?.endDate);
});
const getWorkspaceWithDeployments_createServerFn_handler = createServerRpc({
  id: "dfafee004ed7d22984f781684a1598955aa523268e3cc7484a59340099c142fe",
  name: "getWorkspaceWithDeployments",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => getWorkspaceWithDeployments.__executeServer(opts, signal));
const getWorkspaceWithDeployments = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(getWorkspaceWithDeployments_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getWorkspaceWithDeployments(context.accessToken, data.workspaceId);
});
const createWorkspace_createServerFn_handler = createServerRpc({
  id: "d224d4fdf04d1e3e4a674679ea8eb220e2ac03a42bccc670c275299a5b34ebbb",
  name: "createWorkspace",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => createWorkspace.__executeServer(opts, signal));
const createWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  name: string()
})).handler(createWorkspace_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.createWorkspace(context.accessToken, data.name);
});
const deleteWorkspace_createServerFn_handler = createServerRpc({
  id: "584ddf048f5e25684cfdedb6a111d33d9990da0f80efc4aa5296667a2e2b16ba",
  name: "deleteWorkspace",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => deleteWorkspace.__executeServer(opts, signal));
const deleteWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(deleteWorkspace_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.deleteWorkspace(context.accessToken, data.workspaceId);
});
const leaveWorkspace_createServerFn_handler = createServerRpc({
  id: "dfb014fb8961c4a2c21046711ad4132b7bc01683f7aea85e559ccadf9b59074b",
  name: "leaveWorkspace",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => leaveWorkspace.__executeServer(opts, signal));
const leaveWorkspace = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(leaveWorkspace_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.leaveWorkspace(context.accessToken, data.workspaceId);
});
const getWorkspaceMembers_createServerFn_handler = createServerRpc({
  id: "32b21247079fabfb00161910619ea8d49a0c821bc1e52f58b9f44e05a0316cb8",
  name: "getWorkspaceMembers",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => getWorkspaceMembers.__executeServer(opts, signal));
const getWorkspaceMembers = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(getWorkspaceMembers_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getWorkspaceMembers(context.accessToken, data.workspaceId);
});
const inviteUser_createServerFn_handler = createServerRpc({
  id: "58d3f30f3787a28021574cd304f4a850c4b29f7264055a165cd3c82490f6d22e",
  name: "inviteUser",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => inviteUser.__executeServer(opts, signal));
const inviteUser = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  email: string().email(),
  role: _enum(["owner", "admin", "member"]).default("member")
})).handler(inviteUser_createServerFn_handler, async ({
  context,
  data
}) => {
  const acceptanceUrl = `${env.APP_URL}/workspaces`;
  return lazycloudApi.inviteUser(context.accessToken, data.workspaceId, data.email, data.role, acceptanceUrl);
});
const updateMemberRole_createServerFn_handler = createServerRpc({
  id: "4354af4503203ea6de8683625f263bcfb011c10f5479ef2d0f5fedce99470ec6",
  name: "updateMemberRole",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => updateMemberRole.__executeServer(opts, signal));
const updateMemberRole = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  memberUserId: string(),
  role: _enum(["owner", "admin", "member"])
})).handler(updateMemberRole_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.updateMemberRole(context.accessToken, data.workspaceId, data.memberUserId, data.role);
});
const removeMember_createServerFn_handler = createServerRpc({
  id: "0c0e9585a6426bc7600440ff22e273a212c349e755d04a10f61b79deeb543e3d",
  name: "removeMember",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => removeMember.__executeServer(opts, signal));
const removeMember = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  memberUserId: string()
})).handler(removeMember_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.removeMember(context.accessToken, data.workspaceId, data.memberUserId);
});
const transferOwnership_createServerFn_handler = createServerRpc({
  id: "3db7e11bd713090ede30750b2d216db34688cda58e3dd7333cd04db815fca1d2",
  name: "transferOwnership",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => transferOwnership.__executeServer(opts, signal));
const transferOwnership = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  newOwnerUserId: string()
})).handler(transferOwnership_createServerFn_handler, async ({
  context,
  data
}) => {
  const acceptanceUrl = `${env.APP_URL}/workspaces`;
  return lazycloudApi.transferOwnership(context.accessToken, data.workspaceId, data.newOwnerUserId, acceptanceUrl);
});
const acceptInvitation_createServerFn_handler = createServerRpc({
  id: "1936ab0787592dbe5c35b27e824fad5d7dacb95e65b1e39bfac31a1554615bb6",
  name: "acceptInvitation",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => acceptInvitation.__executeServer(opts, signal));
const acceptInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  invitationId: string()
})).handler(acceptInvitation_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.acceptInvitation(context.accessToken, data.invitationId);
});
const declineInvitation_createServerFn_handler = createServerRpc({
  id: "e8e785839e5a7187627ca075c80e31d36e365950fc1a9019edd5f458e2bb89f8",
  name: "declineInvitation",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => declineInvitation.__executeServer(opts, signal));
const declineInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  invitationId: string()
})).handler(declineInvitation_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.declineInvitation(context.accessToken, data.invitationId);
});
const getPendingInvitations_createServerFn_handler = createServerRpc({
  id: "7b75b999221fc8cf129b5f999b3f07bfa19ef1788d33daad39e0fcd8813934c9",
  name: "getPendingInvitations",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => getPendingInvitations.__executeServer(opts, signal));
const getPendingInvitations = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).handler(getPendingInvitations_createServerFn_handler, async ({
  context
}) => {
  return lazycloudApi.getPendingInvitations(context.accessToken);
});
const cancelInvitation_createServerFn_handler = createServerRpc({
  id: "7163a8b51474f579f981b34cc539a1c2087035b0b75c4166ed0963394fb8f6be",
  name: "cancelInvitation",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => cancelInvitation.__executeServer(opts, signal));
const cancelInvitation = createServerFn({
  method: "POST"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string(),
  invitationId: string()
})).handler(cancelInvitation_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.cancelInvitation(context.accessToken, data.workspaceId, data.invitationId);
});
const getPendingOwnershipTransfer_createServerFn_handler = createServerRpc({
  id: "03aac44a215ebd59084d08ffbecee7e3f79eb34d1f12159b7b26cef6cb320f5f",
  name: "getPendingOwnershipTransfer",
  filename: "src/server/functions/workspaces.ts"
}, (opts, signal) => getPendingOwnershipTransfer.__executeServer(opts, signal));
const getPendingOwnershipTransfer = createServerFn({
  method: "GET"
}).middleware([authMiddleware]).inputValidator(object({
  workspaceId: string()
})).handler(getPendingOwnershipTransfer_createServerFn_handler, async ({
  context,
  data
}) => {
  return lazycloudApi.getPendingOwnershipTransfer(context.accessToken, data.workspaceId);
});
export {
  acceptInvitation_createServerFn_handler,
  cancelInvitation_createServerFn_handler,
  createWorkspace_createServerFn_handler,
  declineInvitation_createServerFn_handler,
  deleteWorkspace_createServerFn_handler,
  getPendingInvitations_createServerFn_handler,
  getPendingOwnershipTransfer_createServerFn_handler,
  getWorkspaceMembers_createServerFn_handler,
  getWorkspaceWithDeployments_createServerFn_handler,
  getWorkspaces_createServerFn_handler,
  inviteUser_createServerFn_handler,
  leaveWorkspace_createServerFn_handler,
  removeMember_createServerFn_handler,
  transferOwnership_createServerFn_handler,
  updateMemberRole_createServerFn_handler
};
