# Platform core

- Own shared cluster/network infrastructure, registry, identities, Argo and node
  capacity. Review plans and preserve persistent resources.
- Bootstrap node capacity before Argo depends on it; use real resource requests
  without incidental hardware pins.
- Root Argo tracks main. Preserve operator-set child application automation pauses.
- Keep Pod Identity and secret-reader OIDC permissions distinct. Shared images are
  immutable commit artifacts; cluster API CIDRs belong only here.
