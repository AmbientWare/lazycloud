# Production Deployment Notes

## Required Configuration

### kube-state-metrics Label Exposure

- Add label allowlist: `lazycloud.io/service`, `lazycloud.io/deployment-id`, `lazycloud.io/workspace-id`, `app.kubernetes.io/instance`
- Verify labels appear in Prometheus `kube_pod_labels` metric

### Volume Metrics (EKS Only)

- Verify CSI drivers are installed and `kubelet_volume_stats_used_bytes` metric is available
- If missing, storage usage will be 0 with logged warnings