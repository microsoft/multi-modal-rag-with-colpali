# First Run

Deploy the demo, index a small PDF, and ask a question about it. Run the commands
below in **PowerShell 7** from the repository root.

> [!WARNING]
> This provisions paid Azure resources, including AKS nodes, GPU compute, disks,
> monitoring, and model usage. Charges continue while resources remain deployed.
> The default Agent UI is publicly accessible over HTTP without sign-in; use only
> non-sensitive demo documents. Delete the demo resource group when finished.

> [!IMPORTANT]
> The default model and indexer now use the same 320-dimensional pooled-vector
> contract. Existing indexes with the old schema or another model require a new
> collection and coordinated reindex/cutover; follow the
> [index migration guide](../modules/document_processor/README.md#migrating-an-existing-index)
> before upgrading an existing deployment. A fresh deployment creates the new schema.

## 1. Prerequisites

- Install Git, PowerShell 7, Azure CLI (with Bicep), kubectl, Helm, and Docker.
  Start Docker; the build script uses `az acr login`, although builds run in ACR.
- Use an Azure subscription where you can create resources and role assignments
  (for example, Owner, or Contributor plus User Access Administrator).
- Check regional availability and quota for `Standard_D4s_v3`, `Standard_D8s_v3`,
  and Spot `Standard_NV18ads_A10_v5`. The GPU pool can be evicted.
  The template also deploys `gpt-4.1` Global Standard with capacity 100 in **UK South**;
  check that model's quota there before deploying.
- Python 3.14 and uv are needed only for local development/tests.

```powershell
git clone https://github.com/microsoft/multi-modal-rag-with-colpali.git
Set-Location multi-modal-rag-with-colpali
az login
az account set --subscription "<your-subscription-id>"

$ResourceGroup = "colpali-demo-rg"
$Location = "uksouth" # Choose a region with the required VM availability and quota.
az group create --name $ResourceGroup --location $Location

helm repo add qdrant https://qdrant.github.io/qdrant-helm
helm repo add reloader https://stakater.github.io/stakater-charts
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
```

Edit `modules\infra\src\main.bicepparam`: replace `baseName = 'colpali'` with a
short, globally unique lowercase alphanumeric name, such as `colpalidemo123`.
Resource names, including the storage account and registry, are derived from it.

## 2. Deploy

Run each command only after the previous one succeeds:

```powershell
.\scripts\deploy_infra.ps1 -ResourceGroup $ResourceGroup
.\scripts\build_all.ps1
.\scripts\apply_helm.ps1
```

`deploy_infra.ps1` uses the current Azure CLI subscription by default. To select a
different subscription without changing the CLI default:

```powershell
.\scripts\deploy_infra.ps1 -ResourceGroup $ResourceGroup -Subscription "your subscription name"
```

The script resolves the subscription and tenant dynamically, then derives your
tenant-local user object ID from that subscription's ARM token. This avoids using
a guest user's home-directory ID for role assignments. No identity IDs are stored
in the script, and tokens are not logged or written to disk. An interactive
`az login` user session is required. Deployment and AKS credential retrieval use
the same resolved subscription.

Resource names still come from `modules/infra/src/main.bicepparam`; changing only
`-ResourceGroup` does not change the resource base name. The script writes the
repository `.env`, updates the default kubeconfig, and applies the AKS bootstrap
manifests. Failed bootstrap commands stop the script rather than report success.

For an isolated Helm deployment, pass a separate configuration file and kubeconfig:

```powershell
.\scripts\apply_helm.ps1 -EnvFile "C:\path\test.env" `
    -Subscription "your subscription name" -KubeConfig "C:\path\test.kubeconfig"
```

Only the specified environment file and kubeconfig are updated. Without these
options, the script retains its repository `.env` and default kubeconfig behavior.
Helm dependencies are restored from the chart lockfile.

For batching, concurrency, DPI, model limits, and GPU memory settings, use the
[Helm tuning guide](../modules/helm/colpali-stack/README.md#indexing-and-inference-tuning).
The deployment script's `.env` contains connection settings and image tags; it
does not automatically forward arbitrary application environment variables.
Apply tuning through chart values and preserve those overrides on later upgrades.

The scripts provision infrastructure, write connection settings to the ignored
root `.env` file, build five images in ACR, and deploy the Helm chart. Do not commit
`.env`. The first deployment can take tens of minutes while nodes start and model
weights download. `apply_helm.ps1` prints the Agent UI URL.

```powershell
kubectl get pods -n colpali-stack
```

Wait for the application pods to be ready. Do not use `-LocalAccess` for this guide:
despite its name, that switch also exposes inference and Qdrant through external
load balancers.

## 3. Upload a PDF and Ask a Question

Place a small, non-sensitive PDF at `.\sample.pdf`. Use a document with a fact or
table you can ask about. The default event filter watches `.pdf` files in the
`documents` container.

```powershell
$config = Get-Content .env -Raw | ConvertFrom-StringData
az storage blob upload `
    --account-name $config.DATA_STORAGE_ACCOUNT_NAME `
    --container-name documents `
    --name sample.pdf --file .\sample.pdf `
    --auth-mode login --overwrite true

kubectl logs -n colpali-stack -l app=colpali-stack-document-processor --tail=50 -f
```

Look for `Document complete: pages=... indexed=... skipped=... duration=...`,
then press Ctrl+C to stop following logs. Open the printed Agent UI URL and ask
a specific question answered by your PDF. Expect an answer with a source filename
and page number; inspect the cited page to confirm it matches.

`indexed` reports the pages present in the completed index, including reused
pages; `skipped` identifies pages that did not need embedding. An unchanged
document can therefore report equal nonzero `indexed` and `skipped` counts.
Completion logs are not an atomic publication or a substitute for checking
Service Bus settlement. Index optimization may lag ingestion before approximate
search includes all segments.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Deployment fails on names, quota, or permissions | Read the Azure CLI error; choose a unique base name, request the named quota, or obtain resource/role-assignment permissions before retrying. |
| Pods stay Pending or model startup is slow | Run `kubectl get events -n colpali-stack --sort-by=.metadata.creationTimestamp`; check GPU Spot capacity, node scaling, and volume mounts. |
| Upload returns 403 | Confirm the active subscription/user and allow time for Storage Blob Data Contributor to propagate. Also inspect the account's network rules and applicable policies; use an approved network path rather than weakening access controls. |
| Secret mounts fail with `ForbiddenByConnection` | Check Key Vault network access, DNS/private connectivity, and applicable policies. Resolve the intended networking configuration; a pod restart or role assignment alone does not fix a network denial. |
| Upload succeeds but no answer is found | Check processor logs above. Inspect inference logs with `kubectl logs -n colpali-stack statefulset/colpali-stack-colpali-inference -c vllm --tail=50`. Check the Service Bus queue's dead-letter messages in the portal; after fixing the cause, re-upload the PDF. |
| Qdrant rejects dimensions, names, or legacy payloads | The current contract requires 320-dimensional `hierarchical_pooled`, `mean_row_pooled`, and `mean_column_pooled` vectors. Reindex old data into a new collection and switch writer/reader configuration together; do not delete an existing collection to make deployment succeed. |

## 4. Clean Up

This deletes **everything in the demo resource group**, including uploaded PDFs,
the index, and model resources. Confirm the group name and keep any data you need:

```powershell
az group delete --name $ResourceGroup
```

Wait for deletion to complete. Helm uninstall alone does not remove the Azure
infrastructure or stop all charges. The local `.env` now points to deleted resources.
