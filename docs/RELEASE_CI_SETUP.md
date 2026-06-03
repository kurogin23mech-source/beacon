# Release CI setup (e-909 / e-910)

How a change reaches users, which parts are automated, and the one-time human
setup the two new workflows need before they can fire.

Companion to the CORE doc **"Beacon リリースとデプロイ手順 (人間 / AI / 自動の
責務分担)"** (`3mNEuZdrtrUpDNq4Yj4L`). This file is the operational appendix:
the secrets/variables and the GCP setup that doc references.

## Why this exists — the propagation matrix

Beacon is one product made of five components that reach users through
different channels. New work is only "shipped" when **every** relevant channel
is updated, and the manual steps are where things silently get missed:

| Component | Maintainer side | User side | Automated by |
|-----------|-----------------|-----------|--------------|
| **Web UI** | Cloud Run deploy | nothing (instant) | `deploy-cloud-run.yml` (e-910) |
| **Server API** | Cloud Run deploy | nothing | `deploy-cloud-run.yml` (e-910) |
| **CLI** | bump version, tag, formula, CHANGELOG | `brew upgrade` / `beacon update` | `release.yml` (e-909) + `release-build.yml` |
| **Skills** | bundled into the wheel | `beacon skill install` (via `beacon update`) | packaged by `release-build.yml`; install is user-side |
| **CLAUDE.md fragment** | update the snippet `beacon init` injects | re-run in the user's project | (still manual — see "Not yet automated") |
| **Desktop (Tauri)** | build bundles | reinstall the app | `release-build.yml` / `desktop-release.yml` |

The goal of e-909 + e-910 is that **the maintainer side has no hand-run steps
left** — one `workflow_dispatch` does the metadata release, and a push to `main`
deploys the server. The user side stays a single `beacon update`.

## The three release workflows (who does what)

| Workflow | Trigger | Does |
|----------|---------|------|
| `release.yml` (e-909) | manual `workflow_dispatch` | runs `scripts/release.py`: bump `__version__`, tag `vX.Y.Z`, GitHub release, formula sha256, **mirror to homebrew-beacon tap**, CHANGELOG |
| `release-build.yml` (e-696) | `release-v*` tag / dispatch | builds CLI wheel + Desktop bundles for all OS, uploads to a draft GitHub release |
| `deploy-cloud-run.yml` (e-910) | push to `main` (server/lib/…) / dispatch | `gcloud run deploy` + health check |

> **Run order for a full release:** trigger `release.yml` (dry-run first, then
> `dry_run=false`). It tags `vX.Y.Z`. Cloud Run deploy fires automatically from
> the bump commit landing on `main`. Desktop/CLI artifacts: push a matching
> `release-v*` tag (or dispatch `release-build.yml`). Tag-trigger unification is
> a tracked follow-up — see "Not yet automated".

## One-time setup

### 1. `TAP_PUSH_TOKEN` — for `release.yml`

A fine-grained Personal Access Token with **Contents: write** on
`kurogin23mech-source/homebrew-beacon` (the Homebrew tap). `GITHUB_TOKEN` can't
push to a *different* repo, so the tap mirror needs this.

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate.
2. Repository access: only `homebrew-beacon`. Permissions: Contents → Read and write.
3. In the `beacon` repo: Settings → Secrets and variables → Actions → **New repository secret** → name `TAP_PUSH_TOKEN`.

Also confirm `beacon` repo → Settings → Actions → General → Workflow permissions
is **Read and write** (so `release.yml`'s bump commit + tag can be pushed).
If `main` is a protected branch, allow the release bot / `github-actions` to push,
or push the bump via a PAT instead.

### 2. GCP Workload Identity Federation — for `deploy-cloud-run.yml`

No long-lived keys. Federate GitHub's OIDC token to a deploy service account.
Replace `PROJECT_ID` and `PROJECT_NUMBER`:

```bash
# Pool + GitHub provider (once)
gcloud iam workload-identity-pools create github-pool \
  --project=PROJECT_ID --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=PROJECT_ID --location=global \
  --workload-identity-pool=github-pool \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='kurogin23mech-source/beacon'" \
  --issuer-uri="https://token.actions.githubusercontent.com"

# Deploy service account + roles
gcloud iam service-accounts create beacon-deployer --project=PROJECT_ID
for role in roles/run.admin roles/cloudbuild.builds.editor \
            roles/iam.serviceAccountUser roles/storage.admin \
            roles/artifactregistry.admin; do
  gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="serviceAccount:beacon-deployer@PROJECT_ID.iam.gserviceaccount.com" \
    --role="$role"
done

# Let this repo impersonate the SA
gcloud iam service-accounts add-iam-policy-binding \
  beacon-deployer@PROJECT_ID.iam.gserviceaccount.com --project=PROJECT_ID \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/kurogin23mech-source/beacon"
```

Then in the `beacon` repo → Settings → Secrets and variables → Actions:

| Kind | Name | Value |
|------|------|-------|
| **Variable** | `GCP_PROJECT` | your Cloud Run project id (this also un-gates the deploy job) |
| Secret | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider` |
| Secret | `GCP_SERVICE_ACCOUNT` | `beacon-deployer@PROJECT_ID.iam.gserviceaccount.com` |

Until `GCP_PROJECT` is set, `deploy-cloud-run.yml` skips (safe to merge).

## How to cut a release once configured

1. Actions → **Release (CLI metadata + Homebrew tap)** → Run workflow → leave
   `dry_run=true` → inspect the plan.
2. Re-run with `dry_run=false` (optionally pin `version: vX.Y.Z`; blank
   auto-detects from commit prefixes per `version-rules`).
3. `deploy-cloud-run.yml` deploys the server automatically when the bump commit
   lands on `main` (or dispatch it).
4. For Desktop/CLI binaries, push a `release-v*` tag or dispatch `release-build.yml`.
5. Users run `beacon update`.

## Not yet automated (tracked follow-ups)

- **Single trigger / tag unification.** Today `v*` (metadata), `release-v*`
  (artifacts) and the Cloud Run deploy fire on different events. A single
  `workflow_dispatch` fanning out to all three needs care: `release.py` and
  `release-build.yml` both create a GitHub Release for the same tag, which must
  be de-conflicted first.
- **CLAUDE.md fragment propagation.** The snippet `beacon init` writes into a
  user's `CLAUDE.md` is not versioned/auto-updated in existing projects.
- **Desktop auto-install.** Users still reinstall the `.dmg`/`.msi` by hand
  (OS-level, hard to automate — noted in the CORE doc).
