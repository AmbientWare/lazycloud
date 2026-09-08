# OVHcloud Public Cloud

This adapter uses the US API and hourly Gen 3 instances in Virginia and Oregon.
It reads current USD prices and stock from OVHcloud. Local disk and Basic Public
IPv4 prices are separate catalog components, including when their current price
is zero. The adapter creates no floating IPs, block volumes, backups, or monthly
commitments.

Create a Public Cloud project in the US account and copy its project ID from the
project overview. Create an API key at
[the US token page](https://api.us.ovhcloud.com/createToken/). Sub-users use
[the sub-user token page](https://us.ovhcloud.com/auth/api/createToken).
Keep the application key, application secret, and consumer key together in
`LAZYCLOUD_PLATFORM_CAPACITY_OVH_CREDENTIALS`, indexed by the binding reference.
`LAZYCLOUD_PLATFORM_CAPACITY_OVH` contains the project ID and published node images.
See the [official US API setup guide](https://support.us.ovhcloud.com/hc/en-us/articles/360018130839-First-steps-with-the-OVHcloud-API).

For project `PROJECT_ID`, the running provider needs these permissions:

- `GET /cloud/project/PROJECT_ID/flavor`
- `GET /cloud/project/PROJECT_ID/image/*`
- `GET /cloud/project/PROJECT_ID/volume`
- `GET /cloud/project/PROJECT_ID/region/*/instance`
- `GET /cloud/project/PROJECT_ID/region/*/instance/*`
- `GET /cloud/project/PROJECT_ID/operation/*`
- `POST /cloud/project/PROJECT_ID/region/*/instance`
- `DELETE /cloud/project/PROJECT_ID/instance/*`

Image publishing also needs `GET /cloud/project/PROJECT_ID/image`,
`GET /cloud/project/PROJECT_ID/snapshot`,
`POST /cloud/project/PROJECT_ID/region/*/instance/*/snapshot`, and
`DELETE /cloud/project/PROJECT_ID/snapshot/*`. A publisher can build a disposable
instance and snapshot it through the same API credentials. Uploading a prebuilt
disk through OpenStack is a separate operation and is not required by this adapter.

Each enabled region needs a private node image named
`lazycloud-node-<recipe_sha256>-<bake_id>`. The binding records its UUID and full recipe hash.
Publish the node runtime before enabling the binding. Provider API keys never go
into these images or onto workers. Each launch receives a short-lived bootstrap
token and creates its own node credential.

Create and read use the current region-scoped API. Deletion uses the documented
project-instance endpoint, since the region endpoint has no DELETE operation.
The [live API schema](https://api.us.ovhcloud.com/1.0/cloud.json) describes both.
The adapter records asynchronous operation IDs before polling. If a create times
out before it returns an operation ID, it keeps the launch unresolved and refuses
to create a replacement. Cleanup continues looking for that exact launch and
cannot report deletion complete while the outcome is unknown.

Instance deletion removes its local disk and Basic Public IP. An unexpected
attached persistent volume stops deletion so an operator can resolve its owner.
Shared private images remain billable until their publisher retires them.

Provider acceptance requires a disposable instance, worker enrollment, a real
CPU workload, instance deletion, and an inventory check for leftover resources.
The owner tests cover launch accounting and cleanup decisions. They do not prove
that an account has quota, images, network access, or working host artifacts.
