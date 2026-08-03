// Infrastructure for the weather streaming pipeline.
//
//   az deployment group create \
//     --resource-group rg-weather-streaming \
//     --template-file infra/main.bicep \
//     --parameters infra/main.parameters.json
//
// The deployment is idempotent: run it as often as you like. Two resources are
// reused rather than created, because they already exist and cost money to
// recreate — see `existingEventHubNamespaceName` and `existingKeyVaultName`.

targetScope = 'resourceGroup'

@description('Region for new resources. Keep this equal to the existing Event Hub namespace region to avoid cross-region egress.')
param location string = 'eastus'

@description('Prefix used to name every resource. Lowercase letters only.')
@minLength(3)
@maxLength(8)
param namePrefix string = 'weather'

@description('Deployment environment, surfaced in logs and the health endpoint.')
@allowed(['dev', 'prod'])
param environmentName string = 'prod'

@description('Event Hub namespace that already exists in this resource group.')
param existingEventHubNamespaceName string = 'hb-weatherstreamingnamespace'

@description('Key Vault that already exists in this resource group. Must use RBAC authorization.')
param existingKeyVaultName string = 'kv-weatherstreaming-1'

@description('Name of the event hub to create inside the existing namespace.')
param eventHubName string = 'weather-events'

@description('Comma-separated locations to poll.')
param weatherLocations string = 'Tokyo,Osaka,Sapporo'

@description('Name of the Key Vault secret holding the weatherapi.com key.')
param weatherApiKeySecretName string = 'weatherapi'

@description('NCRONTAB schedules (6 fields: second minute hour day month day-of-week).')
param ingestCurrentSchedule string = '*/30 * * * * *'
param ingestForecastSchedule string = '0 */30 * * * *'
param curateSchedule string = '0 5 * * * *'

@description('Email address for alerts. Leave empty to skip creating an action group.')
param alertEmail string = ''

@description('Monitoring thresholds.')
param alertMaxTempC int = 38
param alertMinTempC int = -10
param alertMaxWindKph int = 60
param alertMaxPm25 int = 55
param alertMaxUsEpaIndex int = 4

@description('Days before raw bronze data moves to the Cool tier.')
param bronzeCoolAfterDays int = 30

@description('Days before raw bronze data moves to Archive. Reading it then requires rehydration.')
param bronzeArchiveAfterDays int = 90

@description('Days before raw bronze data is deleted. 730 mirrors a two-year compliance retention.')
param bronzeRetentionDays int = 730

@description('Days before the curated silver layer moves to the Cool tier.')
param silverCoolAfterDays int = 90

@description('Create the Static Web App that hosts the public dashboard.')
param deployDashboard bool = true

@description('Static Web Apps is only offered in a handful of regions; eastus is not one of them.')
@allowed(['westus2', 'centralus', 'eastus2', 'westeurope', 'eastasia'])
param dashboardLocation string = 'eastus2'

// Six characters of entropy keeps storage account names inside the 24-character
// limit while staying globally unique.
var suffix = take(uniqueString(resourceGroup().id), 6)
var functionAppName = 'func-${namePrefix}-${suffix}'
var runtimeStorageName = 'st${namePrefix}fn${suffix}'
var lakeStorageName = 'st${namePrefix}lake${suffix}'
var tags = {
  project: 'weather-streaming-app'
  environment: environmentName
  managedBy: 'bicep'
}

// The Flex Consumption host stores its deployment package here.
var deploymentContainerName = 'deploymentpackage'

// Built-in role definition ids (verified against this subscription).
var roleIds = {
  eventHubDataSender: '2b629674-e913-4c01-ae53-ef4638d8f975'
  eventHubDataReceiver: 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

// ---------------------------------------------------------------------------
// Existing resources
// ---------------------------------------------------------------------------

resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' existing = {
  name: existingEventHubNamespaceName
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: existingKeyVaultName
}

// ---------------------------------------------------------------------------
// Event Hub
// ---------------------------------------------------------------------------

// Basic tier caps retention at 1 day and allows only the $Default consumer
// group. That is enough here: the archive function drains the hub into blob
// storage continuously, so the hub is a buffer, not a store.
resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: eventHubName
  properties: {
    partitionCount: 2
    messageRetentionInDays: 1
  }
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-${suffix}'
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

// The Functions runtime needs its own account and does not support accounts
// with hierarchical namespace enabled, so the lake gets a separate one.
resource runtimeStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: runtimeStorageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource runtimeBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: runtimeStorage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: runtimeBlobService
  name: deploymentContainerName
}

// ADLS Gen2 (hierarchical namespace) so that Hive-style folder partitions are
// real directories — Power BI, Fabric and Spark all read this layout natively.
resource lakeStorage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: lakeStorageName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource lakeBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: lakeStorage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource bronzeContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: lakeBlobService
  name: 'bronze'
}

resource silverContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: lakeBlobService
  name: 'silver'
}

resource servingContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: lakeBlobService
  name: 'serving'
}

// Storage that only ever grows is how a log platform dies on cost rather than
// on architecture — the problem that sank the original version of this idea.
// Each layer's retention is therefore a decision, not a default.
//
// `serving` is deliberately absent: three small files, rewritten hourly and
// read on every page load, so they must stay Hot.
resource lakeLifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: lakeStorage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'bronze-tier-and-expire'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['bronze/']
            }
            actions: {
              baseBlob: {
                // Curation only ever reads the last 24 hours, so raw data is
                // cold almost immediately after it lands. Archive is safe for
                // the same reason: nothing in this pipeline reads it, and a
                // compliance retrieval can afford a rehydration wait.
                tierToCool: {
                  daysAfterModificationGreaterThan: bronzeCoolAfterDays
                }
                tierToArchive: {
                  daysAfterModificationGreaterThan: bronzeArchiveAfterDays
                }
                delete: {
                  daysAfterModificationGreaterThan: bronzeRetentionDays
                }
              }
            }
          }
        }
        {
          name: 'silver-tier'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['silver/']
            }
            actions: {
              baseBlob: {
                // Power BI reads this layer, so it is cooled but never archived
                // and never deleted — and it is rebuildable from bronze anyway.
                tierToCool: {
                  daysAfterModificationGreaterThan: silverCoolAfterDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${namePrefix}-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    // The free tier covers 5 GB/month; 30 days is plenty for this volume.
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${namePrefix}-${suffix}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Function App
// ---------------------------------------------------------------------------

// Flex Consumption (FC1). The classic Y1 Consumption plan is unavailable on
// this subscription — Visual Studio subscriptions carry a zero VM quota, which
// Y1 and every App Service tier draw against. Flex uses a different quota pool,
// so it is not a preference here but the only serverless option that deploys.
// It also cold-starts faster and scales per-instance-memory rather than per-VM.
resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: 'plan-${namePrefix}-${suffix}'
  location: location
  tags: tags
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp'
  properties: {
    reserved: true // Linux
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    // A user-assigned identity is created and granted its roles *before* the
    // app exists, which removes the ordering problem a system-assigned identity
    // has: the app needs storage access at first start, but a system-assigned
    // principal only comes into being once the app is created.
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${runtimeStorage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: appIdentity.id
          }
        }
      }
      scaleAndConcurrency: {
        // 40 instances is far more than this workload needs; the cap exists so
        // a runaway retry loop cannot quietly consume the whole credit.
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      cors: {
        // The dashboard is served from a different origin than the API. These
        // endpoints are anonymous, read-only and expose public weather data,
        // so a wildcard origin gives up nothing; credentials stay disabled.
        allowedOrigins: ['*']
        supportCredentials: false
      }
      appSettings: [
        // Identity-based storage access: no account keys anywhere in config.
        {
          name: 'AzureWebJobsStorage__accountName'
          value: runtimeStorage.name
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: appIdentity.properties.clientId
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          // Tells DefaultAzureCredential in our own code which user-assigned
          // identity to present.
          name: 'AZURE_CLIENT_ID'
          value: appIdentity.properties.clientId
        }
        {
          name: 'APP_ENVIRONMENT'
          value: environmentName
        }

        // --- Weather API ---
        {
          name: 'WEATHER_LOCATIONS'
          value: weatherLocations
        }
        {
          name: 'KEY_VAULT_URL'
          value: keyVault.properties.vaultUri
        }
        {
          name: 'WEATHER_API_KEY_SECRET_NAME'
          value: weatherApiKeySecretName
        }

        // --- Schedules ---
        {
          name: 'INGEST_CURRENT_SCHEDULE'
          value: ingestCurrentSchedule
        }
        {
          name: 'INGEST_FORECAST_SCHEDULE'
          value: ingestForecastSchedule
        }
        {
          name: 'CURATE_SCHEDULE'
          value: curateSchedule
        }

        // --- Event Hub (identity-based; no connection strings anywhere) ---
        {
          name: 'EVENT_HUB_ENABLED'
          value: 'true'
        }
        {
          name: 'EVENT_HUB_NAMESPACE'
          value: '${existingEventHubNamespaceName}.servicebus.windows.net'
        }
        {
          name: 'EVENT_HUB_NAME'
          value: eventHubName
        }
        {
          name: 'EVENT_HUB_CONSUMER_GROUP'
          value: '$Default'
        }
        {
          // The event-hub trigger resolves its connection from this prefix.
          name: 'EVENT_HUB_CONNECTION__fullyQualifiedNamespace'
          value: '${existingEventHubNamespaceName}.servicebus.windows.net'
        }
        {
          name: 'EVENT_HUB_CONNECTION__credential'
          value: 'managedidentity'
        }
        {
          name: 'EVENT_HUB_CONNECTION__clientId'
          value: appIdentity.properties.clientId
        }

        // --- Lake ---
        {
          name: 'STORAGE_ENABLED'
          value: 'true'
        }
        {
          name: 'STORAGE_ACCOUNT_URL'
          value: lakeStorage.properties.primaryEndpoints.blob
        }
        {
          name: 'STORAGE_BRONZE_CONTAINER'
          value: 'bronze'
        }
        {
          name: 'STORAGE_SILVER_CONTAINER'
          value: 'silver'
        }
        {
          name: 'STORAGE_SERVING_CONTAINER'
          value: 'serving'
        }

        // --- Monitoring thresholds ---
        {
          name: 'ALERT_MAX_TEMP_C'
          value: string(alertMaxTempC)
        }
        {
          name: 'ALERT_MIN_TEMP_C'
          value: string(alertMinTempC)
        }
        {
          name: 'ALERT_MAX_WIND_KPH'
          value: string(alertMaxWindKph)
        }
        {
          name: 'ALERT_MAX_PM2_5'
          value: string(alertMaxPm25)
        }
        {
          name: 'ALERT_MAX_US_EPA_INDEX'
          value: string(alertMaxUsEpaIndex)
        }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Role assignments — granted to the user-assigned identity. No connection
// strings, no account keys, no Key Vault access policies.
// ---------------------------------------------------------------------------

// The Functions host itself needs the runtime storage account for three
// separate things: the deployment package (blob), the timer singleton lock
// (blob) and Event Hub checkpoints (blob + table), plus queues for internal
// bookkeeping.
var runtimeStorageRoles = [
  roleIds.storageBlobDataOwner
  roleIds.storageQueueDataContributor
  roleIds.storageTableDataContributor
]

resource runtimeStorageAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in runtimeStorageRoles: {
    scope: runtimeStorage
    name: guid(runtimeStorage.id, appIdentity.id, roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: appIdentity.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

// Sender for the ingest functions, receiver for the archive function.
var eventHubRoles = [
  roleIds.eventHubDataSender
  roleIds.eventHubDataReceiver
]

resource eventHubAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in eventHubRoles: {
    scope: eventHub
    name: guid(eventHub.id, appIdentity.id, roleId)
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
      principalId: appIdentity.properties.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource writeToLake 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: lakeStorage
  name: guid(lakeStorage.id, appIdentity.id, roleIds.storageBlobDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roleIds.storageBlobDataContributor
    )
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource readSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, appIdentity.id, roleIds.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roleIds.keyVaultSecretsUser
    )
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Alerting
// ---------------------------------------------------------------------------

var createAlerts = !empty(alertEmail)

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (createAlerts) {
  name: 'ag-${namePrefix}-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'weatherops'
    enabled: true
    emailReceivers: [
      {
        name: 'owner'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// Data has stopped flowing. This is the alert that matters most: everything
// else can look healthy while the pipeline quietly ingests nothing.
resource noDataAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (createAlerts) {
  name: 'alert-${namePrefix}-no-ingest'
  location: location
  tags: tags
  properties: {
    displayName: 'Weather pipeline: no successful ingest in 15 minutes'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [appInsights.id]
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "INGEST_CURRENT" | summarize Count = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

resource failureAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (createAlerts) {
  name: 'alert-${namePrefix}-failures'
  location: location
  tags: tags
  properties: {
    displayName: 'Weather pipeline: repeated function failures'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [appInsights.id]
    criteria: {
      allOf: [
        {
          query: 'requests | where success == false | summarize Count = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'GreaterThan'
          threshold: 5
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// The business-level alert: a reading crossed a critical threshold. This is the
// direct analogue of alerting on a critical syslog line.
resource breachAlert 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (createAlerts) {
  name: 'alert-${namePrefix}-threshold-breach'
  location: location
  tags: tags
  properties: {
    displayName: 'Weather pipeline: critical threshold breach detected'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT10M'
    windowSize: 'PT10M'
    scopes: [appInsights.id]
    criteria: {
      allOf: [
        {
          // startswith, not contains: KQL's contains is case-insensitive, so it
          // also matches the archive function's own "wrote ... bronze/
          // threshold_breach/..." lines. Only severityLevel keeps those out
          // today, which would stop being true the moment a failed blob write
          // logged that path as an error.
          query: 'traces | where message startswith "THRESHOLD_BREACH" and severityLevel >= 3 | summarize Count = count()'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: {
      actionGroups: [actionGroup.id]
    }
  }
}

// ---------------------------------------------------------------------------
// Public dashboard
// ---------------------------------------------------------------------------

resource staticSite 'Microsoft.Web/staticSites@2023-12-01' = if (deployDashboard) {
  name: 'swa-${namePrefix}-${suffix}'
  location: dashboardLocation
  tags: tags
  sku: {
    name: 'Free'
    tier: 'Free'
  }
  properties: {
    // Content is pushed by GitHub Actions using the deployment token rather
    // than by binding this resource to the repository.
    buildProperties: {
      appLocation: 'dashboard'
      outputLocation: ''
    }
  }
}

// ---------------------------------------------------------------------------
// Outputs — everything the deploy workflow and the docs need
// ---------------------------------------------------------------------------

output functionAppName string = functionApp.name
output functionAppUrl string = 'https://${functionApp.properties.defaultHostName}'
output healthCheckUrl string = 'https://${functionApp.properties.defaultHostName}/health'
output functionPrincipalId string = appIdentity.properties.principalId
output functionClientId string = appIdentity.properties.clientId
output eventHubNamespaceFqdn string = '${existingEventHubNamespaceName}.servicebus.windows.net'
output eventHubName string = eventHub.name
output lakeStorageAccount string = lakeStorage.name
output lakeBlobEndpoint string = lakeStorage.properties.primaryEndpoints.blob
output keyVaultUri string = keyVault.properties.vaultUri
output appInsightsName string = appInsights.name
output dashboardUrl string = deployDashboard ? 'https://${staticSite!.properties.defaultHostname}' : ''
output dashboardResourceName string = deployDashboard ? staticSite!.name : ''
