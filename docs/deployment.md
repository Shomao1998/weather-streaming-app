# Deployment

## First deployment

### 1. Prerequisites

```bash
az login
az account set --subscription "<your subscription>"
```

The template expects an Event Hub namespace and a Key Vault to already exist in the resource group;
both are reused rather than recreated. Change `existingEventHubNamespaceName` and
`existingKeyVaultName` in `infra/main.parameters.json` if yours are named differently, or create
them first:

```bash
az group create --name rg-weather-streaming --location eastus
az eventhubs namespace create -g rg-weather-streaming -n <namespace> --sku Basic
az keyvault create -g rg-weather-streaming -n <vault> --enable-rbac-authorization true
```

### 2. Store the weather API key

The only secret in the system. Get a free key from [weatherapi.com](https://www.weatherapi.com/).

```bash
az keyvault secret set \
  --vault-name kv-weatherstreaming-1 \
  --name weatherapi \
  --value "<your key>"
```

This needs `Key Vault Secrets Officer` or `Key Vault Administrator` on the vault. Subscription
`Owner` is **not** sufficient — RBAC-enabled vaults separate the management plane from the data
plane.

### 3. Deploy the infrastructure

Preview first; the template is idempotent but `what-if` is free:

```bash
az deployment group what-if \
  --resource-group rg-weather-streaming \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json

az deployment group create \
  --resource-group rg-weather-streaming \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json \
  --parameters alertEmail="you@example.com"
```

Pass `alertEmail` to create the action group and the three alert rules; leave it out and they are
skipped.

### 4. Deploy the code

```bash
FUNC=$(az functionapp list -g rg-weather-streaming --query "[0].name" -o tsv)
cd src/functions && func azure functionapp publish "$FUNC" --python
```

Or push to `main` once CI authentication is configured.

### 5. Verify

```bash
curl "https://$(az functionapp list -g rg-weather-streaming --query '[0].defaultHostName' -o tsv)/health"
```

A healthy response reports the environment, the configured locations, and whether the Event Hub and
storage sinks are enabled. Then confirm data is moving:

```bash
LAKE=$(az storage account list -g rg-weather-streaming --query "[?contains(name,'lake')].name" -o tsv)
az storage blob list --account-name "$LAKE" --container-name bronze --auth-mode login -o table
```

The serving documents appear after the first `curate` run, which is on the hour. Until then the
dashboard shows "Awaiting first curation" — that is the expected state, not a failure.

## Continuous deployment

`.github/workflows/deploy.yml` authenticates with federated credentials: GitHub exchanges an OIDC
token for an Azure token, so no client secret is stored anywhere.

```bash
./scripts/setup_github_oidc.sh
```

The script creates the application registration, grants `Contributor` and
`Role Based Access Control Administrator` on the resource group (the second is required because the
Bicep template creates role assignments), registers the federated credentials, writes the three
repository secrets, and deletes the obsolete publish-profile secret.

### If the tenant will not let you register an application

Guest accounts usually cannot. Two options:

**Ask a tenant administrator** to run the script, or to create the app registration and grant you
ownership.

**Fall back to a publish profile.** This authenticates only the code deployment; infrastructure
changes stay manual, which is a defensible split — Bicep changes are deliberate and infrequent.

```bash
FUNC=$(az functionapp list -g rg-weather-streaming --query "[0].name" -o tsv)
az functionapp deployment list-publishing-profiles \
  --name "$FUNC" -g rg-weather-streaming --xml > profile.xml
gh secret set AZURE_FUNCTIONAPP_PUBLISH_PROFILE < profile.xml
rm profile.xml
```

Then in `deploy.yml`, drop the `azure/login` steps and the `infrastructure` job, and give
`Azure/functions-action` a `publish-profile` and a literal `app-name`. Note that publish profiles
require SCM basic authentication to be enabled on the app, which is a real security downgrade
compared with federated credentials — prefer OIDC where the tenant allows it.

## Troubleshooting

**`SubscriptionIsOverQuotaForSku` on the hosting plan.** The subscription has no VM quota, which
`Y1` and every App Service tier consume. The template already uses Flex Consumption (`FC1`), which
draws on a different pool. If Flex is also refused, the region does not offer it — check
`az functionapp list-flexconsumption-locations`.

**The app deploys but no functions appear.** The host logs `0 functions loaded` and
`No job functions found`, and every route 404s. Three distinct causes, all seen on this project:

1. *Packaging.* `host.json` and `function_app.py` must sit at the root of the deployed directory,
   and no `function.json` may exist anywhere — mixing the v1 and v2 programming models makes the
   host discover nothing while reporting success. `tests/test_function_app.py` asserts both.
2. *A binding annotation the worker cannot resolve.* `cardinality=MANY` on an Event Hub trigger
   must be annotated `typing.List[func.EventHubEvent]`; the PEP 585 form `list[...]` fails
   indexing, and `from __future__ import annotations` in that file breaks it the same way. There is
   no error message — indexing simply returns zero, which takes down **every** function in the app,
   not just the offending one.
3. *Comparing against a known-good app.* When it is unclear which of the two applies, publish a
   three-line hello-world to the same Function App. If that registers, the platform, plan, identity
   and deployment path are all fine and the fault is in the code.

**A function hangs until the timeout with no error.** `Timeout value of 00:05:00 was exceeded` and
nothing else. A blocking call with no logging around it — reach for a phase log first. Two causes
here were a non-reentrant lock whose accessors nested (fixed with `RLock`, see
`tests/test_clients.py`) and `DefaultAzureCredential` probing credential sources that do not
fail fast inside a Function App (fixed by using `ManagedIdentityCredential` when `AZURE_CLIENT_ID`
is set).

**Sub-minute timers never fire.** A timer with `use_monitor=True` persists a status blob on every
tick, which cannot keep up with a schedule faster than once a minute — the function simply never
runs. Set `use_monitor=False` for those; keep it on for the slower timers, where it gives catch-up
after a restart.

**`ImportError: Please install websocket-client`.** The Event Hubs SDK does not pull in
`websocket-client`, and `TransportType.AmqpOverWebsocket` needs it at send time, not import time —
so it surfaces as a runtime failure long after a clean deployment.

**The host logs bury the application's.** With `logging.logLevel.default` at `Information`, the
Azure SDK logs every HTTP request it makes; at a 30-second cadence that is thousands of blob
lease-renewal lines an hour. Keep the default at `Warning` and raise the categories you care about.
Note that a `"//"` comment key inside `logLevel` is parsed as a category name and throws.

**`ManagedIdentityCredential authentication unavailable`.** The role assignment has not propagated;
it can take several minutes. If it persists, confirm `AZURE_CLIENT_ID` in the app settings matches
the user-assigned identity's client id.

**Event Hub trigger never fires.** Check the `EVENT_HUB_CONNECTION__*` settings resolve to the right
namespace, and that the identity holds `Azure Event Hubs Data Receiver` on the hub itself, not only
on the namespace.

**The dashboard shows "Unreachable".** CORS or a wrong `apiBase`. `dashboard/config.js` is rewritten
by the deploy workflow; if you deployed the dashboard by hand, it still points at the sample data.

## Enabling retrieval-grounded advice (v2)

Optional, and off by default. With `RAG_ENABLED` unset the advice card behaves
exactly as it did in v1.1 — the deployment steps above need no changes.

The Bicep template deliberately does **not** provision Azure OpenAI or Azure AI
Search. Both are meaningful recurring costs, and AI Search Standard is the
service that exhausted this subscription's credit; making them a side effect of
`az deployment group create` would be a trap. They are opt-in, by hand.

### 1. Build the knowledge index

```bash
pip install -r requirements-dev.txt
python scripts/ingest_knowledge.py
python scripts/ingest_knowledge.py --check   # must pass before you deploy
```

The index is committed, so this is only needed after editing `knowledge/`.

### 2. Grant the managed identity access

No keys. The Function App's user-assigned identity needs:

```bash
IDENTITY=$(az identity show -g rg-weather-streaming -n id-weatherstreaming \
  --query principalId -o tsv)

# Azure OpenAI
az role assignment create --assignee "$IDENTITY" \
  --role "Cognitive Services OpenAI User" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account>"

# Azure AI Search, only if you use it instead of the local index
az role assignment create --assignee "$IDENTITY" \
  --role "Search Index Data Reader" \
  --scope "/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Search/searchServices/<service>"
```

### 3. Turn it on

```bash
az functionapp config appsettings set -g rg-weather-streaming -n func-weatherstreaming \
  --settings RAG_ENABLED=true \
             RAG_OPENAI_ENDPOINT="https://<account>.openai.azure.com" \
             RAG_CHAT_DEPLOYMENT="gpt-4o-mini" \
             RAG_EMBEDDING_DEPLOYMENT="text-embedding-3-small"
```

Leave `RAG_SEARCH_ENDPOINT` unset to retrieve from the committed local index —
which costs nothing and is the supported configuration for this project. Set it
only if you have provisioned a search service, and then:

```bash
export AZURE_SEARCH_ENDPOINT="https://<service>.search.windows.net"
python scripts/build_search_index.py --create-index
python scripts/build_search_index.py --upload
```

`--upload` refuses to run on an index built with the offline embedder. Rebuild
with `RAG_EMBEDDING_DEPLOYMENT` set first, or the vector half of the index would
be noise.

### 4. Verify

```bash
curl -s "https://<app>.azurewebsites.net/api/advice?location=Tokyo" | jq '.generation_method, .sources'
```

`"rag-v1"` with a populated `sources` array means it is grounded.
`"template-v1"` means something fell back — the reason is in Application
Insights:

```kusto
traces
| where message startswith "ADVICE_RAG_FALLBACK"
| project timestamp, customDimensions.fallback_reason, customDimensions.trigger
| order by timestamp desc
```

A fallback is not an error. It is the system doing what it was designed to do;
the log tells you which dependency was unavailable.

## Tearing it down

Everything created by the template is in one resource group, but the group also holds the
pre-existing Event Hub namespace and Key Vault:

```bash
# Just the expensive part — stops the meter, keeps the data
az eventhubs namespace delete -g rg-weather-streaming -n hb-weatherstreamingnamespace

# Everything, including the reused resources and the lake
az group delete --name rg-weather-streaming
```
