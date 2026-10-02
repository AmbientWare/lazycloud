| Measure | Reference | Rewrite |
| --- | --- | --- |
| deploy, cached image, ms p50/p95/p99 | 1275.9 / 1350.6 / 1350.6 | 578.9 / 1327 / 1327 |
| cold .remote() ms | 1318.4 / 1425 / 1425 | 866.5 / 1053.5 / 1053.5 |
| cold phase admit_to_demand_ms (median of runs) | 31.8 | 3.2 |
| cold phase placement_ms (median of runs) | 31.8 | 7.3 |
| cold phase container_ready_ms (median of runs) | 721.8 | 676.9 |
| cold phase execution_ms (median of runs) | 25.7 | 6.6 |
| warm .remote() ms | 117.9 / 140.9 / 249 | 12.9 / 16.8 / 25.2 |
| warm queue-to-start ms (median) | 35.6 | 2.3499999999999996 |
| map 200: admission/s, total s, results/s, failures | 46.6, 12.11, 16.5, 0 | 1723.7, 2.26, 88.6, 0 |
| map 200: queue wait ms, server span s, containers | 3444 / 4146.2 / 4171.8, 8.29, 8 | 1724.8 / 1799.9 / 1808, 1.82, 6 |
| map 200: platform CPU %, PG exec ms/s, PG stmts/s, PG bytes/s | 324.27, 136.44, 2374.95, 2316867 | 97.0, 216.51, 4672.13, 1459031 |
| map 2000: admission/s, total s, results/s, failures | 63.2, 101.34, 19.7, 0 | 7301.3, 4.07, 491.5, 0 |
| map 2000: queue wait ms, server span s, containers | 12138.9 / 19845.4 / 20370.5, 52.01, 8 | 740.3 / 1367.2 / 1425.2, 1.52, 8 |
| map 2000: platform CPU %, PG exec ms/s, PG stmts/s, PG bytes/s | 294.15, 140.77, 2618.96, 2123667 | 359.41, 1067.23, 25865.06, 7467944 |
| map 10000: admission/s, total s, results/s, failures | - | 8826.1, 27.03, 369.9, 0 |
| map 10000: queue wait ms, server span s, containers | 51764.3 / 88909.3 / 93040.3, 271.73, 8 | 8067.4 / 13155.1 / 13644.2, 14.59, 8 |
| map 10000: platform CPU %, PG exec ms/s, PG stmts/s, PG bytes/s | - | 377.65, 2108.53, 19031.25, 5461348 |
| backlog admission/s per workspace, no capacity | 35.1, 35.3 | 6015.7, 6370.9 |
| backlog first start after release s, drain s | 49.51, 669.69 | 2.61, 9.6 |
| backlog start rate/s | 3.2 | 286.1 |
| backlog age at start s | 601.2 / 689.7 / 697.8 | 36.3 / 39.3 / 39.6 |
| first-quarter share per workspace | [0.0, 1.0] | [0.522, 0.478] |
| endpoint warm ms | 129 / 154 / 258.2 | 1.3 / 1.7 / 2 |
| endpoint cold ms | 903.3 / 993.4 / 993.4 (0 failed) | 779.6 / 790.9 / 790.9 (0 failed) |
| SSE first event ms (x1) | 306 / 410.5 / 443.5 | 1.9 / 2.8 / 5.5 |
| SSE inter-event ms (x1, sent every 50) | 0 / 256 / 262.8 | 50.2 / 50.5 / 51.3 |
| SSE total ms (x20 parallel, ideal 1000) | 1506.1 / 1703.5 / 1781.9 (0 failed) | 1030.8 / 1040.9 / 1042.1 (0 failed) |
| 1000 concurrent, round 1: wall s, latency ms, codes | 24.52, 13803.1 / 23121.7 / 23629.4, {'200': 956, '503': 38, '500': 6} | 14.04, 10253.8 / 13718.6 / 13878, {'200': 1000} |
| 1000 concurrent, round 2: wall s, latency ms, codes | 24.7, 16347.9 / 22542.3 / 23335, {'200': 993, '500': 7} | 9.64, 4300.1 / 7684.5 / 8873.3, {'200': 1000} |
| 1000 concurrent, round 3: wall s, latency ms, codes | 25.11, 15442.6 / 22437.3 / 23333.9, {'200': 1000} | 2.58, 1162.7 / 2220.3 / 2339.3, {'200': 1000} |
| idle default: CPU %, mem MiB, PG stmts/s, PG xact/s, PG bytes/s, Redis cmds/s | 40.18, 3335.5, 107.88, 60.83, 113791, 89.95 | 3.36, 569.5, 8.62, 7.35, 4201, 0 |
| idle default: PG rows scanned/s (tup_returned) | 211.48 | 205.42 |
| idle schedulers-1: CPU %, mem MiB, PG stmts/s, PG xact/s, PG bytes/s, Redis cmds/s | 40.56, 2993.7, 107.54, 59.88, 112433, 68.59 | 3.22, 603.5, 8.59, 7.07, 3350, 0 |
| idle schedulers-1: PG rows scanned/s (tup_returned) | 202.43 | 221.0 |
| idle schedulers-2: CPU %, mem MiB, PG stmts/s, PG xact/s, PG bytes/s, Redis cmds/s | 40.29, 3304.7, 107.99, 60.08, 114109, 90.05 | 3.25, 714.6, 8.67, 6.71, 3323, 0 |
| idle schedulers-2: PG rows scanned/s (tup_returned) | 199.82 | 185.91 |
| idle history-100k: CPU %, mem MiB, PG stmts/s, PG xact/s, PG bytes/s, Redis cmds/s | 41.3, 3484.9, 107.15, 60.07, 111946, 89.68 | 3.19, 638.2, 8.62, 6.63, 2947, 0 |
| idle history-100k: PG rows scanned/s (tup_returned) | 221.21 | 143.17 |
| map 2000 schedulers-2: total s, server span s, platform CPU %, PG exec ms/s | 101.76, 53.08, 334.03, 132.2 | 6.4, 3.44, 280.61, 872.73 |
| map 2000 history-100k: total s, server span s, platform CPU %, PG exec ms/s | 102.39, 53.88, 333.04, 117.74 | 6.03, 3.31, 250.49, 713.5 |

Reference idle processes: agent 6.05%/72MiB, api-ingress 0.26%/18MiB, cache-server 0.69%/26MiB, connection-gateway 6.5%/113MiB, connection-gateway-1 7.05%/109MiB, connection-ingress 0.8%/50MiB, control-plane 1.92%/485MiB, execution-api 1.86%/610MiB, fleet-controller 0.64%/158MiB, fleet-controller-2 0.72%/165MiB, log-retention 0.48%/0MiB, object-store 0.42%/3MiB, otel-collector 0.07%/26MiB, pgbouncer 0.74%/2MiB, postgres 1.56%/39MiB, redis 0.68%/412MiB, runtime-api 5.52%/528MiB, scheduler 1.35%/176MiB, scheduler-2 1.18%/178MiB, worker-1 0.88%/154MiB, workload-registry 0.81%/9MiB
Rewrite idle processes: agent 0.01%/34MiB, object-store 1.1%/3MiB, postgres 2.17%/386MiB, registry 0.01%/11MiB, scheduler 0.03%/43MiB, server 0.04%/93MiB
