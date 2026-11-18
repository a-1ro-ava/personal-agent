// Supporting resources for the personal information-gathering agent.
// Kept outside infra/ on purpose: the microsoft.foundry provider compiles any Bicep under infra/.
// The Foundry account/project, model deployments and Hosted Agent are managed by azd
// (infra.provider: microsoft.foundry). This template is deployed by scripts/postprovision.ps1
// into the same resource group.
targetScope = 'resourceGroup'

@description('Region for Foundry IQ (serverless Azure AI Search), Blob, Cosmos DB and Functions.')
param location string = 'japaneast'

@description('Name of the existing Foundry (AI Services) account created by azd in this resource group.')
param aiAccountName string

@description('Object ID of the developer running the deployment. Granted data-plane roles for local runs.')
param userPrincipalId string = ''

@description('Principal type for userPrincipalId.')
@allowed([ 'User', 'ServicePrincipal' ])
param userPrincipalType string = 'User'

param tags object = {
  workload: 'personal-agent'
}

var token = toLower(uniqueString(resourceGroup().id))
var cosmosDatabaseName = 'personal-agent'
var rawContainerName = 'raw'
var contentContainerName = 'content'
var artifactsContainerName = 'artifacts'
var deploymentContainerName = 'app-package'

var roles = {
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageQueueDataContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  storageBlobDelegator: 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a'
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  cognitiveServicesUser: 'a97b65f3-24c7-4388-baec-2e87135dc908'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
}

// Cosmos DB built-in data plane role: "Cosmos DB Built-in Data Contributor"
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'

// ---------------------------------------------------------------------------
// Monitoring
// ---------------------------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-pa-${token}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-pa-${token}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
    DisableLocalAuth: true
  }
}

// ---------------------------------------------------------------------------
// Blob Storage: raw originals, normalized content (indexed), HTML artifacts, Functions package
// ---------------------------------------------------------------------------
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stpa${token}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days: 7
    }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for name in [ rawContainerName, contentContainerName, artifactsContainerName, deploymentContainerName ]: {
    parent: blobService
    name: name
    properties: { publicAccess: 'None' }
  }
]

// ---------------------------------------------------------------------------
// Cosmos DB (serverless): sources, items, runs, conversations, web search logs, candidates
// ---------------------------------------------------------------------------
resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: 'cosmos-pa-${token}'
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [ { name: 'EnableServerless' } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-11-15' = {
  parent: cosmos
  name: cosmosDatabaseName
  properties: {
    resource: { id: cosmosDatabaseName }
  }
}

// ttl -1 enables per-item "ttl" without expiring items by default; 0 leaves TTL disabled.
var cosmosContainers = [
  { name: 'sources', pk: '/id', ttl: 0 }
  { name: 'items', pk: '/sourceId', ttl: 0 }
  { name: 'runs', pk: '/runDate', ttl: -1 }
  { name: 'conversations', pk: '/sessionId', ttl: -1 }
  { name: 'searchlogs', pk: '/sessionId', ttl: -1 }
  { name: 'candidates', pk: '/id', ttl: 0 }
]

resource cosmosContainerRes 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-11-15' = [
  for c in cosmosContainers: {
    parent: cosmosDb
    name: c.name
    properties: {
      resource: union(
        {
          id: c.name
          partitionKey: {
            paths: [ c.pk ]
            kind: 'Hash'
          }
        },
        c.ttl == 0 ? {} : { defaultTtl: c.ttl }
      )
    }
  }
]

// ---------------------------------------------------------------------------
// Foundry IQ (Azure AI Search, serverless)
// ---------------------------------------------------------------------------
resource search 'Microsoft.Search/searchServices@2026-09-01-preview' = {
  name: 'srch-pa-${token}'
  location: location
  tags: tags
  sku: { name: 'serverless' }
  identity: { type: 'SystemAssigned' }
  properties: {
    disableLocalAuth: true
    publicNetworkAccess: 'enabled'
    knowledgeRetrieval: 'free'
  }
}

// ---------------------------------------------------------------------------
// Azure Functions (Flex Consumption) for the 06:00 JST collection job
// ---------------------------------------------------------------------------
resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-pa-${token}'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: { reserved: true }
}

var functionAppName = 'func-pa-${token}'

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainerName}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD' }
        { name: 'STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
        { name: 'RAW_CONTAINER', value: rawContainerName }
        { name: 'CONTENT_CONTAINER', value: contentContainerName }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'COSMOS_DATABASE', value: cosmosDatabaseName }
        { name: 'SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
        { name: 'KNOWLEDGE_SOURCE_NAME', value: 'content-blob-ks' }
      ]
    }
  }
}

// ---------------------------------------------------------------------------
// Role assignments
// ---------------------------------------------------------------------------
resource aiAccount 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: aiAccountName
}

// Search managed identity -> read Blob content (pull-type ingestion)
resource searchBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, search.id, roles.storageBlobDataReader)
  properties: {
    principalId: search.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.storageBlobDataReader)
  }
}

// Search managed identity -> call embedding / chat models on the Foundry account
resource searchAiRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in [ roles.cognitiveServicesOpenAIUser, roles.cognitiveServicesUser ]: {
    scope: aiAccount
    name: guid(aiAccount.id, search.id, r)
    properties: {
      principalId: search.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    }
  }
]

// Function App managed identity -> host storage + data, Search indexer run, telemetry
resource funcStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in [ roles.storageBlobDataOwner, roles.storageQueueDataContributor, roles.storageTableDataContributor ]: {
    scope: storage
    name: guid(storage.id, functionApp.id, r)
    properties: {
      principalId: functionApp.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    }
  }
]

resource funcSearchRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in [ roles.searchServiceContributor, roles.searchIndexDataContributor ]: {
    scope: search
    name: guid(search.id, functionApp.id, r)
    properties: {
      principalId: functionApp.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    }
  }
]

resource funcMetrics 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: appInsights
  name: guid(appInsights.id, functionApp.id, roles.monitoringMetricsPublisher)
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
  }
}

resource funcCosmos 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, functionApp.id, cosmosDataContributorRoleId)
  properties: {
    principalId: functionApp.identity.principalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    scope: cosmos.id
  }
}

// Developer -> data-plane access for local runs and setup scripts
resource userStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in [ roles.storageBlobDataContributor, roles.storageBlobDelegator ]: if (!empty(userPrincipalId)) {
    scope: storage
    name: guid(storage.id, userPrincipalId, r)
    properties: {
      principalId: userPrincipalId
      principalType: userPrincipalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    }
  }
]

resource userSearch 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for r in [ roles.searchServiceContributor, roles.searchIndexDataContributor ]: if (!empty(userPrincipalId)) {
    scope: search
    name: guid(search.id, userPrincipalId, r)
    properties: {
      principalId: userPrincipalId
      principalType: userPrincipalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', r)
    }
  }
]

resource userCosmos 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = if (!empty(userPrincipalId)) {
  parent: cosmos
  name: guid(cosmos.id, userPrincipalId, cosmosDataContributorRoleId)
  properties: {
    principalId: userPrincipalId
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    scope: cosmos.id
  }
}

// ---------------------------------------------------------------------------
// Outputs (written into the azd environment by scripts/postprovision.ps1)
// ---------------------------------------------------------------------------
output SEARCH_SERVICE_NAME string = search.name
output SEARCH_ENDPOINT string = 'https://${search.name}.search.windows.net'
output SEARCH_PRINCIPAL_ID string = search.identity.principalId
output STORAGE_ACCOUNT_NAME string = storage.name
output STORAGE_ACCOUNT_URL string = storage.properties.primaryEndpoints.blob
output RAW_CONTAINER string = rawContainerName
output CONTENT_CONTAINER string = contentContainerName
output ARTIFACTS_CONTAINER string = artifactsContainerName
output COSMOS_ACCOUNT_NAME string = cosmos.name
output COSMOS_ENDPOINT string = cosmos.properties.documentEndpoint
output COSMOS_DATABASE string = cosmosDatabaseName
output FUNCTION_APP_NAME string = functionAppName
output SUPPORT_LOCATION string = location
