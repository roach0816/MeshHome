# Kubernetes install (K3s with Rancher Continuous Delivery)

This page covers the full container install on K3s with Rancher Continuous Delivery (Fleet), and
running it day to day. The [README](../README.md#option-2-container) has the short version, plus
Docker Compose for a single machine.

- [How the deployment is organised](#how-the-deployment-is-organised)
- [Prerequisites](#prerequisites)
- [Step by step](#step-by-step)
- [Operations](#operations)
- [Deploying from your own fork](#deploying-from-your-own-fork)

## How the deployment is organised

Rancher Continuous Delivery (Fleet) watches the `deploy/k8s` directory of a Git repository and keeps
the cluster in sync with it. That directory is a Kustomize bundle containing:

- the app Deployment and Service;
- a single-instance PostgreSQL Deployment and Service;
- a daily database dump CronJob.

Each build of `main` publishes a multi-arch image (`linux/amd64` + `linux/arm64`) and commits that
image's commit SHA into `deploy/k8s/deployment.yaml`, so Fleet rolls out every release automatically.
Images are always pinned to a commit SHA, never `:latest`.

Anything specific to your environment is created **once by hand** from the `*.example.yaml`
templates, and is deliberately not part of the bundle:

| Resource | Template | Why it is outside the bundle |
| --- | --- | --- |
| Namespace | `namespace.example.yaml` | Removing the bundle must never delete the namespace and its data |
| Database Secret | `secrets.example.yaml` | Secrets never go in Git |
| Volumes (PVCs) | `pvc.example.yaml` | They need your StorageClass, and data must outlive the bundle |
| Ingress | `ingress.example.yaml` | Hostname, ingress class, and certificate issuer are site-specific |

`fleet.yaml` also sets `keepResources: true`, so deleting the Git repo from Rancher leaves running
workloads in place.

## Prerequisites

- A Rancher-managed K3s cluster. Arm64 (e.g. Raspberry Pi 5) and amd64 nodes both work.
- A StorageClass for the database and dumps. If you use NFS, see the requirements in step 3.
- An ingress controller, plus cert-manager with a ClusterIssuer, for HTTPS.
- A DNS name for the app that resolves on your private network.
- Network reachability from the cluster nodes to the radio gateway's TCP port.

Run the `kubectl` commands below from the Rancher **kubectl shell** (the `>_` icon at the top right of
the cluster view) or any shell with access to the cluster.

## Step by step

### 1. Look up your cluster's values

```bash
kubectl get storageclass     # which class to use for the database and dumps
kubectl get ingressclass     # e.g. traefik or nginx — check, don't assume
kubectl get clusterissuer    # the cert-manager issuer for the certificate
```

### 2. Create the namespace and database Secret

Create the namespace in Rancher (**Cluster → Projects/Namespaces → Create Namespace**, name
`meshhome`), or apply `deploy/k8s/namespace.example.yaml`. Then:

```bash
PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
kubectl -n meshhome create secret generic meshhome-db --from-literal=POSTGRES_PASSWORD="$PW"
echo "$PW"   # keep this in a password manager
```

The password must be URL-safe, because the app embeds it in its database URL. `token_urlsafe` output
is URL-safe.

### 3. Create the volumes

Copy `deploy/k8s/pvc.example.yaml`, replace `<YOUR_NFS_STORAGECLASS>` with your StorageClass, and apply
it. Use Rancher's **Import YAML** button (top right), or **Storage → PersistentVolumeClaims → Create →
Edit as YAML**. This creates `meshhome-db-data` and `meshhome-backups`, 10 GiB each; adjust the sizes
as needed.

If the database volume is on NFS:

- **Ownership:** the database directory must be owned by **UID/GID 999** (the `postgres` user in the
  official image). After the claim binds, find its directory on the NFS server and run
  `chown -R 999:999 <dir>`. With root-squash enabled, the pod cannot fix ownership itself.
- **Mount and export:** the mount must be `hard` (check with `mount | grep nfs` on a node), and the
  export must honour synchronous writes. If your NFS server can't meet that, put `meshhome-db-data` on
  local or block storage and use NFS only for `meshhome-backups`.

### 4. Add the Git repo to Continuous Delivery

In Rancher: **☰ → Continuous Delivery → Git Repos**. In the workspace dropdown, choose
`fleet-default` to deploy to a downstream cluster, or `fleet-local` if Rancher runs on the same
cluster. Then **Add Repository**:

| Field | Value |
| --- | --- |
| Name | `meshhome` |
| Repository URL | `https://github.com/roach0816/MeshHome.git` (or your fork) |
| Branch | `main` |
| Paths | `deploy/k8s` |
| Deploy To | your K3s cluster |

A public repository needs no Git credentials. Click **Create**, and wait for the Git repo and its
bundle to show **Active/Ready**. Fleet polls for changes about once a minute.

The same thing as YAML (for **Edit as YAML**):

```yaml
apiVersion: fleet.cattle.io/v1alpha1
kind: GitRepo
metadata:
  name: meshhome
  namespace: fleet-default          # or fleet-local
spec:
  repo: https://github.com/roach0816/MeshHome.git
  branch: main
  paths: [deploy/k8s]
  targets:
    - clusterName: <YOUR_CLUSTER_NAME>
```

Check the rollout:

```bash
kubectl -n meshhome get pods                       # meshhome and meshhome-db Running 1/1
kubectl -n meshhome get deploy meshhome -o jsonpath='{..image}{"\n"}'
```

### 5. Run the setup wizard

Get the one-time setup token: open **Workloads → Deployments → meshhome → ⋮ → View Logs** in
Rancher, or run:

```bash
kubectl -n meshhome logs deploy/meshhome | grep -A3 "setup token"
```

You can finish setup before the Ingress exists. Run
`kubectl -n meshhome port-forward svc/meshhome 8080:80`, then open <http://localhost:8080>. In the
wizard, choose **MeshCore TCP** if the gateway is already on the network, or **Restore a backup
instead** to bring over another installation. Otherwise choose **Simulated** or **Decide later**, and
connect the radio afterwards (see [Radios](radios.md#connecting-a-tcp-gateway)).

### 6. Create the Ingress

The app doesn't need to be told its hostname. You choose it here, and it lives only in the Ingress
and in your DNS.

**a. Choose the hostname and find where its DNS record should point.** The A record for the hostname
points at your **ingress controller**, not at the app pod:

```bash
HOST=meshhome.example.com     # the fully qualified name you want to use
kubectl get svc -A | grep -iE 'traefik|ingress'
```

Use the controller Service's `EXTERNAL-IP`. On K3s, the bundled Traefik is exposed by ServiceLB
(klipper), which usually lists your node IPs; any of them works, and a virtual IP (e.g. kube-vip or
MetalLB) is best if you have one. If you already have another app behind the same ingress, its record
points to the same place:

```bash
kubectl get ingress -A        # the ADDRESS column shows the IP(s) existing hosts use
```

**b. Create the DNS record** `HOST → that IP` on your **private** DNS (local DNS server or router).
Don't port-forward the app to the internet. Check it from a machine on your network:

```bash
dig +short "$HOST"            # or: nslookup "$HOST"
```

**c. Create the Ingress.** Fill in the three placeholders in `deploy/k8s/ingress.example.yaml`: the
hostname (in both `host:` and `tls.hosts`), the ingress class, and the ClusterIssuer (both from
step 1). Then either:

- **In Rancher:** **Service Discovery → Ingresses → Create → Edit as YAML**, then paste the filled-in
  file. Use the YAML editor rather than the guided form, which can drop `pathType` or the TLS settings.
- **With kubectl**, from a checkout of this repo:
  ```bash
  sed -e "s/<YOUR_HOSTNAME>/$HOST/g" \
      -e "s/<YOUR_INGRESS_CLASS>/traefik/" \
      -e "s/<YOUR_CLUSTER_ISSUER>/<issuer-name>/" \
      deploy/k8s/ingress.example.yaml | kubectl apply -f -
  ```

**d. Verify the certificate issued.** If your hostname is in a public domain but resolves to a
private IP, the ClusterIssuer must use a **DNS-01** solver; HTTP-01 can't reach a private app.

```bash
kubectl -n meshhome get ingress meshhome -o yaml   # cert-manager annotation and tls: block present
kubectl -n meshhome get certificate -w             # wait for READY=True
```

### 7. Verify

Open `https://<YOUR_HOSTNAME>` and sign in. Then check:

- **Realtime updates:** the status line under the inbox name should not say "live updates paused". If
  it does, WebSockets are not getting through the ingress.
- **Server status:** **Settings** shows the radio state, the app version, and any collection gaps.

## Operations

- **Updating:** each successful build of `main` pins a new image, and Fleet rolls it out within a few
  minutes. Pods use `strategy: Recreate`, so expect a short interruption on each rollout. The gateway
  keeps messages it receives in that window queued (its buffer is finite). The app shows when a new
  release exists, but a container is updated by redeploying, not from the UI.
- **Backups:** use **Settings → Backup & restore** for an encrypted backup of everything the app holds
  (see [Backup and restore](backup-restore.md)). The `meshhome-db-backup` CronJob additionally dumps
  the database daily (see that page for restoring a dump).
- **Demo or restore environments:** set `RADIO_ENABLED=false` on the app container so it never
  connects to a radio.
- **Single instance:** keep `replicas: 1` and `strategy: Recreate`. Only one process may own the
  radio; a PostgreSQL advisory lock enforces this.
- **Uninstalling:** delete the Git repo in Continuous Delivery. Because of `keepResources: true`,
  workloads stay in place; delete them, then the namespace, when you're sure you no longer need the
  data.

- **Bot apps that receive uploads:** devices send to `/push/<code>` over plain HTTP. If your
  ingress redirects HTTP to HTTPS, exempt `/push/` or expose the Service on your network; see
  [Bot apps](bot-apps.md#plain-http).

## Moving from the `meshcore` namespace (MeshCore Home 0.9 and earlier)

Since 0.10 the manifests use the MeshHome names: namespace `meshhome`, Deployments `meshhome` and
`meshhome-db`, Secret `meshhome-db`, volumes `meshhome-db-data` and `meshhome-backups`, database and
user `meshhome`. When Fleet picks up 0.10, it removes the old workloads from the `meshcore` namespace
and starts the new ones in `meshhome` with an empty database. The old volumes, Secret and Ingress are
not part of the bundle, so they stay. Move the data like this:

1. **Before updating**, dump the database and keep the file somewhere safe:

   ```bash
   kubectl -n meshcore exec deploy/meshcore-db -- pg_dump -Fc -U meshcore meshcore > meshhome-move.dump
   ```

2. Create the new namespace, a Secret and the volumes (steps 2 and 3 above, with the `meshhome`
   names). You can reuse the old password:

   ```bash
   kubectl create namespace meshhome
   PW=$(kubectl -n meshcore get secret meshcore-db -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d)
   kubectl -n meshhome create secret generic meshhome-db --from-literal=POSTGRES_PASSWORD="$PW"
   ```

3. Let Fleet deploy 0.10 and wait until `meshhome-db` is running. Then load the dump while the app
   is stopped:

   ```bash
   kubectl -n meshhome scale deploy/meshhome --replicas=0
   kubectl -n meshhome exec -i deploy/meshhome-db -- \
     pg_restore --clean --if-exists --no-owner --role=meshhome -U meshhome -d meshhome < meshhome-move.dump
   kubectl -n meshhome scale deploy/meshhome --replicas=1
   ```

4. Recreate the Ingress in the `meshhome` namespace (step 6, same hostname, backend service
   `meshhome`), and delete the old one: `kubectl -n meshcore delete ingress meshcore`.

5. Sign in and check your messages. Then delete the old namespace and its volumes:
   `kubectl delete namespace meshcore`. Keep `meshhome-move.dump` until you're sure.

## Deploying from your own fork

The CI workflow (`.github/workflows/ci.yml`) handles forks automatically. On each push to `main` it:

1. Runs the backend tests against PostgreSQL, lints and builds the frontend, and shellchecks the
   installer.
2. Builds and publishes `ghcr.io/<owner>/<repo>:<commit-sha>` (plus `:latest`) for `linux/amd64` and
   `linux/arm64`.
3. Checks that the image can be pulled without credentials. If it can't (a new GHCR package is
   private), it stops with instructions, so Fleet keeps running the previous image.
4. Commits that image reference into `deploy/k8s/deployment.yaml` as `github-actions[bot]`, with
   `[skip ci]`. This step is skipped if `main` has moved on since the build started.

To deploy from a fork:

- **Wait for the pin:** push to your fork's `main` and wait for the first `deploy: pin image …` commit
  before adding the fork in Continuous Delivery.
- **Make the image pullable:** make the GHCR package public (**GitHub → Packages → your package →
  Package settings → Change visibility**), or give the cluster a pull secret:
  ```bash
  kubectl -n meshhome create secret docker-registry ghcr-pull \
    --docker-server=ghcr.io --docker-username=<GITHUB_USER> --docker-password=<READ_PACKAGES_TOKEN>
  kubectl -n meshhome patch serviceaccount default -p '{"imagePullSecrets":[{"name":"ghcr-pull"}]}'
  ```
  With a pull secret, set the repository variable `ALLOW_PRIVATE_IMAGE` to `true` (**Settings →
  Secrets and variables → Actions → Variables**), so CI pins private images.
- **Release notes link:** set `RELEASE_NOTES_URL` on the app container (for example
  `https://github.com/<owner>/<repo>/releases/tag/v{version}`) so the version number in the app links
  to your fork's releases.
- **Native releases** from a fork are covered in [Development](development.md#releases-and-forks).
