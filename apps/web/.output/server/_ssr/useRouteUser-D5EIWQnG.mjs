import { h as useRouteContext } from "../_chunks/_libs/@tanstack/react-router.mjs";
function useRouteUser() {
  const context = useRouteContext({ from: "__root__" });
  return context.user ?? null;
}
export {
  useRouteUser as u
};
