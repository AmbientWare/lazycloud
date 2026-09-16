export const computeDestinations = [
  {
    key: "managed",
    slot: 5,
    title: "LazyCloud",
    body: "Deploy without managing servers. LazyCloud provisions CPU capacity and scales down when idle.",
  },
  {
    key: "aws",
    slot: 7,
    title: "Your AWS account",
    body: "Use CPU and GPU capacity in your AWS account. Manage deployments through LazyCloud.",
  },
  {
    key: "machines",
    slot: 9,
    title: "Your own machines",
    body: "Connect your Linux servers, VMs, or GPU machines. Deploy through the same Python API.",
  },
] as const;
