#!/usr/bin/env bash
# One-time setup so GitHub Actions can deploy without a stored secret.
#
#   ./scripts/setup_github_oidc.sh
#
# Creates an Entra ID application, grants it Contributor on the resource group,
# and registers federated credentials that let this repository's workflows
# exchange a GitHub OIDC token for an Azure token. No client secret is ever
# created, so there is nothing to rotate and nothing to leak.
#
# Requires: az (logged in), gh (logged in), and permission to register
# applications in the tenant. Guest accounts often do not have that permission —
# if creation fails, see docs/deployment.md for the publish-profile fallback.

set -euo pipefail

APP_NAME="${APP_NAME:-github-weather-streaming-app}"
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-weather-streaming}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$1" >&2; exit 1; }

command -v az >/dev/null || fail "az is not installed."
command -v gh >/dev/null || fail "gh is not installed."

SUBSCRIPTION_ID=$(az account show --query id -o tsv) || fail "Run 'az login' first."
TENANT_ID=$(az account show --query tenantId -o tsv)
REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner) || fail "Run 'gh auth login' first."

info "Subscription : $SUBSCRIPTION_ID"
info "Tenant       : $TENANT_ID"
info "Repository   : $REPO"

az group show --name "$RESOURCE_GROUP" >/dev/null 2>&1 \
  || fail "Resource group '$RESOURCE_GROUP' does not exist. Deploy infra/main.bicep first."

info "Creating (or reusing) the application registration '$APP_NAME'…"
APP_ID=$(az ad app list --display-name "$APP_NAME" --query "[0].appId" -o tsv 2>/dev/null || true)
if [ -z "$APP_ID" ]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv) || fail \
    "Could not create the application. Guest accounts usually cannot register apps in a tenant —
   ask a tenant administrator, or use the publish-profile fallback in docs/deployment.md."
fi
info "Application id: $APP_ID"

az ad sp show --id "$APP_ID" >/dev/null 2>&1 || az ad sp create --id "$APP_ID" >/dev/null
PRINCIPAL_ID=$(az ad sp show --id "$APP_ID" --query id -o tsv)

info "Granting Contributor on '$RESOURCE_GROUP'…"
SCOPE="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP"
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role Contributor \
  --scope "$SCOPE" >/dev/null 2>&1 || info "Role assignment already present."

# Deploying the Bicep template creates role assignments, which Contributor
# cannot do on its own.
info "Granting Role Based Access Control Administrator on '$RESOURCE_GROUP'…"
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Role Based Access Control Administrator" \
  --scope "$SCOPE" >/dev/null 2>&1 || info "Role assignment already present."

add_credential() {
  local name="$1" subject="$2"
  if az ad app federated-credential list --id "$APP_ID" --query "[?name=='$name']" -o tsv | grep -q .; then
    info "Federated credential '$name' already exists."
    return
  fi
  az ad app federated-credential create --id "$APP_ID" --parameters "{
    \"name\": \"$name\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"$subject\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" >/dev/null
  info "Created federated credential '$name'."
}

add_credential "main-branch" "repo:${REPO}:ref:refs/heads/main"
add_credential "pull-requests" "repo:${REPO}:pull_request"

info "Writing repository secrets…"
gh secret set AZURE_CLIENT_ID --body "$APP_ID"
gh secret set AZURE_TENANT_ID --body "$TENANT_ID"
gh secret set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"

# The old workflow authenticated with a publish profile; leaving it behind
# would be a live credential nobody is watching.
if gh secret list | grep -q AZURE_FUNCTIONAPP_PUBLISH_PROFILE; then
  info "Removing the obsolete AZURE_FUNCTIONAPP_PUBLISH_PROFILE secret…"
  gh secret delete AZURE_FUNCTIONAPP_PUBLISH_PROFILE
fi

printf '\n\033[1;32mDone.\033[0m Push to main, or run the Deploy workflow manually.\n'
