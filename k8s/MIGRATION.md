# How this runs on the homelab k8s cluster

Same pattern as `lowfloat_trader`: deployed via Flux (GitOps), independent
`GitRepository` + `Kustomization` applied directly to the cluster, so a
normal `git push` here is all that's needed to change what's running.

## Cluster wiring (one-time, not yet done — do this before the first deploy)

- Create a Flux `GitRepository` + `Kustomization` for this repo (copy the
  pattern used for `lowfloat_trader` in the cluster's own repo), pointed at
  `k8s/` here.
- This repo reuses the **same** `trading` namespace as `lowfloat_trader` —
  `k8s/namespace.yaml` is NOT duplicated here, so `lowfloat_trader` must
  already be deployed (or deploy its namespace.yaml first).
- `.sops.yaml` here reuses the same age recipient as `lowfloat_trader`'s
  (`age1z5dhc09avsv27x5e82vdgyalhakgrek86cc53q794j5w0smygqgsq0zmye`), which
  is already the `sops-age` secret in `flux-system` — no new cluster secret
  needed, Flux will decrypt `k8s/secret.env` automatically on reconcile.
- Add a read-only SSH deploy key for **this** repo (it's presumably
  private, same as `lowfloat_trader`), stored as its own secret in
  `flux-system` (e.g. `tradingbot-deploy-key`) so the `GitRepository` can
  pull it.
- `ghcr.io/parthjp80/tradingbot` needs to be made a **public** package
  after the first image push (Settings → Package → Change visibility),
  same as `lowfloat_trader` — the image has no secrets baked in, only
  `k8s/secret.env` (encrypted) carries real values.
- `nfs-csi` StorageClass already exists on the cluster (shared with
  `lowfloat_trader`'s PVCs) — `k8s/pvc.yaml` here just requests two more
  volumes from it, no new wiring needed.

## Config and secrets — auto-restart on change

`k8s/configmap.env` and `k8s/secret.env` are fed through Kustomize's
`configMapGenerator`/`secretGenerator` (see `k8s/kustomization.yaml`). Every
edit produces a new object with a content-hash suffix
(`tradingbot-config-<hash>`), and Kustomize rewrites every reference to it —
so the Deployment's pod spec actually changes, and Kubernetes rolls the pod
automatically. No `kubectl rollout restart` needed for config/secret
changes.

### Update config (non-secret)

```bash
nano k8s/configmap.env
git add k8s/configmap.env && git commit -m "..." && git push
# Flux reconciles within ~1m; pod rolls automatically
```

### Update secrets (only needed for BOT_BROKER_BACKEND=alpaca_paper)

```bash
sops k8s/secret.env   # opens decrypted in $EDITOR, re-encrypts on save
git add k8s/secret.env && git commit -m "..." && git push
```

### Update bot code

```bash
git push   # to bot/**, main.py, requirements.txt, Dockerfile
# triggers .github/workflows/build.yaml -> new :latest image on GHCR
kubectl -n trading rollout restart deployment/tradingbot
```

## Day-to-day operations

### View logs
```bash
kubectl -n trading logs -f deployment/tradingbot
```

### Check open positions / journal
```bash
kubectl -n trading exec -it deployment/tradingbot -- cat /app/data/paper_positions.json
kubectl -n trading exec -it deployment/tradingbot -- cat /app/data/trade_journal.csv
```

### Run a report manually
```bash
kubectl -n trading exec -it deployment/tradingbot -- python3 -c \
  "from bot.journal import print_report; print_report(period='weekly')"
```

### Manually run a single cycle (e.g. to test after a config change)
```bash
kubectl -n trading exec -it deployment/tradingbot -- python3 main.py --once --force
```

### Check scale-up/-down CronJob history
```bash
kubectl -n trading get jobs -l component=scaler
kubectl -n trading get cronjob tradingbot-scale-up tradingbot-scale-down
```

### Check Flux status for this app
```bash
flux get sources git tradingbot
flux get kustomizations tradingbot
```

## Troubleshooting

**Pod stuck in Pending:**
```bash
kubectl -n trading describe pod <pod-name>
# Usually NFS mount issue — check the nfs-csi StorageClass / TrueNAS export
```

**Image pull error:** confirm the GHCR package is still public:
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://ghcr.io/v2/parthjp80/tradingbot/manifests/latest
```

**Pod not scaling up at market open:** check the CronJob ran and the
ServiceAccount has the expected RBAC (`k8s/scale-rbac.yaml`):
```bash
kubectl -n trading logs job/<tradingbot-scale-up-job-name>
```

**Flux not picking up changes:**
```bash
flux reconcile source git tradingbot
flux reconcile kustomization tradingbot
```
