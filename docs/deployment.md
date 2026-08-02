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

**The app deploys but no functions appear.** Almost always a packaging problem. `host.json` and
`function_app.py` must sit at the root of the deployed directory, and no `function.json` may exist
anywhere — mixing the v1 and v2 programming models causes the host to discover nothing while
reporting success. `tests/test_function_app.py` asserts both.

**`ManagedIdentityCredential authentication unavailable`.** The role assignment has not propagated;
it can take several minutes. If it persists, confirm `AZURE_CLIENT_ID` in the app settings matches
the user-assigned identity's client id.

**Event Hub trigger never fires.** Check the `EVENT_HUB_CONNECTION__*` settings resolve to the right
namespace, and that the identity holds `Azure Event Hubs Data Receiver` on the hub itself, not only
on the namespace.

**The dashboard shows "Unreachable".** CORS or a wrong `apiBase`. `dashboard/config.js` is rewritten
by the deploy workflow; if you deployed the dashboard by hand, it still points at the sample data.

## Tearing it down

Everything created by the template is in one resource group, but the group also holds the
pre-existing Event Hub namespace and Key Vault:

```bash
# Just the expensive part — stops the meter, keeps the data
az eventhubs namespace delete -g rg-weather-streaming -n hb-weatherstreamingnamespace

# Everything, including the reused resources and the lake
az group delete --name rg-weather-streaming
```
