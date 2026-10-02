# Deployment

- deploy/README.md describes the production deployment, what each setting
  comes from and the bring-up runbook. Keep it current with changes here.
- One way to do each thing: Terraform owns infrastructure and the chart's
  infrastructure values, the chart owns runtime settings and secret
  bindings, Ship builds and Deploy records, Argo CD installs. CI never runs
  Terraform or Helm against a cluster.
- Every setting the chart gives a process must be one its binary reads;
  `deploy/check.sh` enforces it. Bind each secret only to the processes
  that read it.
- Pin versions exactly: providers, charts, images by digest, actions by
  commit. Change pins and lock files together.
- Grant IAM for calls the binaries make, scoped to tagged or named
  resources; say in a comment why a wildcard stays.
- Run `deploy/check.sh` before committing. Applies, syncs, bakes and
  workflow runs against real accounts need the user's go-ahead.
