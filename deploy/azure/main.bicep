// =============================================================================
// IFTA Agent — Azure infrastructure (Container Apps + managed Postgres)
// =============================================================================
// Provisions the full production stack for the IFTA filing pipeline:
//   - Azure Container Apps environment + web / worker / telegram-bot apps
//   - Azure Database for PostgreSQL Flexible Server (Burstable) for job state
//   - Storage account + Azure Files shares mounted into the apps (PII + uploads)
//   - Key Vault (RBAC) for all secrets, read by a user-assigned identity
//   - Azure Container Registry (image source), pulled via the same identity
//   - Log Analytics + Application Insights (observability)
//   - A monthly Consumption Budget + email alert (guards the temporary credit)
//
// Deploy (resource-group scope):
//   az group create -n rg-ifta -l eastus
//   az deployment group create -g rg-ifta -f deploy/azure/main.bicep \
//       -p @deploy/azure/main.parameters.json
//
// Validate without deploying:
//   az bicep build -f deploy/azure/main.bicep
//   az deployment group validate -g rg-ifta -f deploy/azure/main.bicep -p @...
//
// Teardown (stops all billing — the whole stack lives in one resource group):
//   az group delete -n rg-ifta --yes --no-wait
//
// First deploy note: the container apps reference an image in ACR that does not
// exist until CI pushes it. The infra provisions fine; the app revisions go
// healthy after the first `deploy-azure.yml` run (Phase 4).
// =============================================================================

// ----------------------------- Parameters ------------------------------------

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Region for PostgreSQL. Some subscriptions are offer-restricted for Flexible Server in the primary region (e.g. eastus) — override to a permitted region. Cross-region to the apps is fine: the AllowAzureServices firewall + SSL still apply.')
param pgLocation string = location

@description('Optional salt appended to the Postgres server name. Use only to sidestep a phantom name-lock: a failed create in a restricted region leaves the deterministic name bound to that location at the ARM placement layer, blocking recreation elsewhere.')
param pgNameSalt string = ''

@description('Short prefix for resource names (lowercase letters/digits).')
@minLength(3)
@maxLength(11)
param namePrefix string = 'ifta'

@description('Deploy the Telegram intake bot as a third container app.')
param deployTelegramBot bool = true

@description('Create the three container apps. Set false for a first-pass infra deploy (registry/DB/vault/etc.) before the image exists, then true once the image is pushed — Container Apps fails to provision against a missing image, so the registry must be populated first.')
param deployContainerApps bool = true

@description('Container image repository name inside ACR.')
param imageRepository string = 'ifta'

@description('Container image tag CI publishes (overridden per deploy by CI).')
param imageTag string = 'latest'

// --- Postgres ---
@description('Postgres administrator login (cannot be "admin"/"azure_superuser").')
param pgAdminLogin string = 'iftaadmin'

@description('Postgres administrator password. Generate one, e.g. `openssl rand -base64 24`.')
@secure()
@minLength(12)
param pgAdminPassword string

@description('Postgres database name for job state.')
param pgDatabaseName string = 'ifta'

// --- App configuration (non-secret) ---
@description('Public base URL used to build confirmation/packet email links (e.g. your custom domain or the web app FQDN).')
param publicBaseUrl string = ''

@description('Comma-separated CORS origins allowed to call the intake API.')
param corsOrigins string = 'https://artjeck.com,https://www.artjeck.com'

@description('Resend "from" address for outbound email.')
param resendFromEmail string = 'ArtJeck IFTA <ifta@artjeck.com>'

@description('BCC address for admin copies of intake email (optional).')
param adminBcc string = ''

@description('Comma-separated Telegram admin numeric user IDs (optional).')
param telegramAdminUserIds string = ''

@description('Comma-separated Telegram admin chat IDs for notifications (optional).')
param telegramAdminChatId string = ''

@description('Agent model for web/telegram review runs.')
param agentModel string = 'claude-sonnet-4-6'

@description('Agent reasoning effort.')
param agentEffort string = 'medium'

// --- Secrets (stored in Key Vault). Default to a sentinel so a first deploy
//     succeeds; set the real values with `az keyvault secret set` afterwards. ---
@description('Anthropic API key (LLM review agent).')
@secure()
#disable-next-line secure-parameter-default
param anthropicApiKey string = 'REPLACE-IN-KEYVAULT'

@description('Resend API key (outbound email).')
@secure()
#disable-next-line secure-parameter-default
param resendApiKey string = 'REPLACE-IN-KEYVAULT'

@description('Cloudflare Turnstile secret key (CAPTCHA verification).')
@secure()
#disable-next-line secure-parameter-default
param turnstileSecretKey string = 'REPLACE-IN-KEYVAULT'

@description('Shared key the Vercel frontend uses to authenticate to the backend.')
@secure()
#disable-next-line secure-parameter-default
param iftaWebBackendKey string = 'REPLACE-IN-KEYVAULT'

@description('Telegram bot token from BotFather.')
@secure()
#disable-next-line secure-parameter-default
param telegramBotToken string = 'REPLACE-IN-KEYVAULT'

// --- Budget / cost guardrail ---
@description('Monthly budget in USD; alerts fire at 80% actual and 100% forecast.')
param monthlyBudget int = 75

@description('Email to receive budget alerts (required — no PII is committed to the repo).')
param alertEmail string

@description('Budget start date — must be the first of a month, YYYY-MM-01.')
param budgetStartDate string = '2026-07-01'

@description('Object ID of the deploying user, granted Key Vault Secrets Officer so this template can seed secret values into the RBAC-enabled vault. Get it with: az ad signed-in-user show --query id -o tsv. Leave empty only if you pre-granted yourself the role.')
param deployerPrincipalId string = ''

@description('Common resource tags.')
param tags object = {
  project: 'ifta'
  environment: 'production'
  managedBy: 'bicep'
}

// ------------------------------- Variables -----------------------------------

var suffix = uniqueString(resourceGroup().id)
var acrName = toLower('${namePrefix}acr${suffix}')
var kvName = take(toLower('${namePrefix}kv${suffix}'), 24)
var saName = take(toLower('${namePrefix}st${suffix}'), 24)
var logName = '${namePrefix}-logs'
var appiName = '${namePrefix}-appi'
var envName = '${namePrefix}-cae'
var pgName = toLower('${namePrefix}-pg-${suffix}${pgNameSalt}')
var uamiName = '${namePrefix}-app-identity'
var image = '${acr.properties.loginServer}/${imageRepository}:${imageTag}'

// Azure built-in role definition IDs.
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var kvSecretsOfficerRoleId = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

// ------------------------------ Observability --------------------------------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appiName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ------------------------------ Identity + ACR -------------------------------

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: uamiName
  location: location
  tags: tags
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false // pull via managed identity, not admin creds
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, uami.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// -------------------------------- Key Vault ----------------------------------

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true // access via role assignments, not policies
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, uami.id, kvSecretsUserRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grant the deploying user data-plane rights to seed secret values. With an
// RBAC vault, Owner/Contributor is NOT enough to write secrets via ARM.
resource kvSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(deployerPrincipalId)) {
  name: guid(kv.id, deployerPrincipalId, kvSecretsOfficerRoleId)
  scope: kv
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficerRoleId)
    principalId: deployerPrincipalId
    principalType: 'User'
  }
}

resource secAnthropic 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'anthropic-api-key'
  properties: { value: anthropicApiKey }
  dependsOn: [kvSecretsOfficer]
}
resource secResend 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'resend-api-key'
  properties: { value: resendApiKey }
  dependsOn: [kvSecretsOfficer]
}
resource secTurnstile 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'turnstile-secret-key'
  properties: { value: turnstileSecretKey }
  dependsOn: [kvSecretsOfficer]
}
resource secBackendKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'ifta-web-backend-key'
  properties: { value: iftaWebBackendKey }
  dependsOn: [kvSecretsOfficer]
}
resource secTelegram 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = if (deployTelegramBot) {
  parent: kv
  name: 'telegram-bot-token'
  properties: { value: telegramBotToken }
  dependsOn: [kvSecretsOfficer]
}

// Postgres DSN (built from the server FQDN + admin creds) so the apps get one
// IFTA_WEB_DB_URL secret. psycopg reads postgresql:// URLs; SSL is required.
resource secDbUrl 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'ifta-web-db-url'
  properties: {
    value: 'postgresql://${pgAdminLogin}:${pgAdminPassword}@${pg.properties.fullyQualifiedDomainName}:5432/${pgDatabaseName}?sslmode=require'
  }
  dependsOn: [kvSecretsOfficer]
}

// -------------------------------- Postgres -----------------------------------

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgName
  location: pgLocation
  tags: tags
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  properties: {
    version: '16'
    administratorLogin: pgAdminLogin
    administratorLoginPassword: pgAdminPassword
    storage: { storageSizeGB: 32 }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: pg
  name: pgDatabaseName
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}

// Allow Azure-internal services (Container Apps) to reach Postgres. The special
// 0.0.0.0 rule means "Azure services", not the public internet. SSL is enforced.
// Hardening follow-up: move both onto a VNet with a private endpoint.
resource pgFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: pg
  name: 'AllowAzureServices'
  properties: { startIpAddress: '0.0.0.0', endIpAddress: '0.0.0.0' }
}

// ------------------------------- Storage -------------------------------------

resource sa 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: saName
  location: location
  tags: tags
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowSharedKeyAccess: true // Azure Files env storage authenticates with the account key
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: sa
  name: 'default'
}

// One share per persistent/PII directory the apps need.
var coreShares = ['submissions', 'clients']
// 'state' persists data/telegram_access.json (approvals) across restarts via
// the IFTA_TELEGRAM_ACCESS_FILE override on the telegram app.
var telegramShares = ['inbox', 'outputs', 'state']
var shareNames = deployTelegramBot ? concat(coreShares, telegramShares) : coreShares

resource shares 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = [for s in shareNames: {
  parent: fileService
  name: s
  properties: { shareQuota: 100, enabledProtocols: 'SMB' }
}]

// -------------------------- Container Apps env -------------------------------

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// Register each Azure Files share as an environment storage the apps can mount.
resource envStorages 'Microsoft.App/managedEnvironments/storages@2024-03-01' = [for (s, i) in shareNames: {
  parent: env
  name: s
  properties: {
    azureFile: {
      accountName: sa.name
      accountKey: sa.listKeys().keys[0].value
      shareName: shares[i].name
      accessMode: 'ReadWrite'
    }
  }
}]

// ------------------------------ Shared app bits ------------------------------

var registriesConfig = [
  {
    server: acr.properties.loginServer
    identity: uami.id
  }
]

// Secrets referenced from Key Vault by the user-assigned identity.
var kvSecretRefs = [
  { name: 'anthropic-api-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/anthropic-api-key', identity: uami.id }
  { name: 'resend-api-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/resend-api-key', identity: uami.id }
  { name: 'turnstile-secret-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/turnstile-secret-key', identity: uami.id }
  { name: 'ifta-web-backend-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/ifta-web-backend-key', identity: uami.id }
  { name: 'ifta-web-db-url', keyVaultUrl: '${kv.properties.vaultUri}secrets/ifta-web-db-url', identity: uami.id }
]

var telegramSecretRef = [
  { name: 'telegram-bot-token', keyVaultUrl: '${kv.properties.vaultUri}secrets/telegram-bot-token', identity: uami.id }
]

// Env vars shared by web + worker (the self-service intake path).
var commonEnv = [
  { name: 'IFTA_WEB_DB_URL', secretRef: 'ifta-web-db-url' }
  { name: 'IFTA_WEB_SUBMISSIONS_DIR', value: '/app/data/web_submissions' }
  { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
  { name: 'RESEND_API_KEY', secretRef: 'resend-api-key' }
  { name: 'RESEND_FROM_EMAIL', value: resendFromEmail }
  { name: 'IFTA_WEB_PUBLIC_BASE_URL', value: publicBaseUrl }
  { name: 'IFTA_WEB_ADMIN_BCC', value: adminBcc }
  { name: 'IFTA_WEB_AGENT_MODEL', value: agentModel }
  { name: 'IFTA_WEB_AGENT_EFFORT', value: agentEffort }
  { name: 'TELEGRAM_ADMIN_CHAT_ID', value: telegramAdminChatId }
  { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
]

var coreVolumes = [
  { name: 'submissions', storageName: 'submissions', storageType: 'AzureFile' }
  { name: 'clients', storageName: 'clients', storageType: 'AzureFile' }
]
var coreVolumeMounts = [
  { volumeName: 'submissions', mountPath: '/app/data/web_submissions' }
  { volumeName: 'clients', mountPath: '/app/data/clients' }
]

// App resources must not start before their identity can pull the image and
// read Key Vault, and before the referenced secrets exist.
var coreDependencies = [acrPull, kvSecretsUser, secAnthropic, secResend, secTurnstile, secBackendKey, secDbUrl]

// --------------------------------- Web app -----------------------------------

resource webApp 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApps) {
  name: '${namePrefix}-web'
  location: location
  tags: tags
  dependsOn: coreDependencies
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: registriesConfig
      secrets: kvSecretRefs
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          command: ['ifta']
          args: ['web', '--host', '0.0.0.0', '--port', '8000']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(commonEnv, [
            { name: 'TURNSTILE_SECRET_KEY', secretRef: 'turnstile-secret-key' }
            { name: 'IFTA_WEB_BACKEND_KEY', secretRef: 'ifta-web-backend-key' }
            { name: 'IFTA_WEB_CORS_ORIGINS', value: corsOrigins }
          ])
          volumeMounts: coreVolumeMounts
          probes: [
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 20
            }
          ]
        }
      ]
      volumes: coreVolumes
      scale: {
        minReplicas: 0 // scale-to-zero: idle web costs nothing (cold start on first hit)
        maxReplicas: 2
        rules: [
          {
            name: 'http-scale'
            http: { metadata: { concurrentRequests: '20' } }
          }
        ]
      }
    }
  }
}

// -------------------------------- Worker app ---------------------------------
// No ingress; a continuous poller, so pinned to exactly one always-on replica.

resource workerApp 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApps) {
  name: '${namePrefix}-worker'
  location: location
  tags: tags
  dependsOn: coreDependencies
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registriesConfig
      secrets: kvSecretRefs
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: image
          command: ['ifta']
          args: ['worker']
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: commonEnv
          volumeMounts: coreVolumeMounts
        }
      ]
      volumes: coreVolumes
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// ------------------------------ Telegram bot ---------------------------------
// No ingress; long-polls Telegram, so one always-on replica. Mounts clients +
// inbox + outputs. NOTE: data/telegram_access.json (approvals) is a hardcoded
// single file under data/ and needs the env-override added in Phase 3 to
// persist across restarts — tracked, not yet wired here.

resource telegramApp 'Microsoft.App/containerApps@2024-03-01' = if (deployContainerApps && deployTelegramBot) {
  name: '${namePrefix}-telegram'
  location: location
  tags: tags
  dependsOn: [acrPull, kvSecretsUser, secAnthropic, secTelegram]
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${uami.id}': {} }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registriesConfig
      secrets: concat([
        { name: 'anthropic-api-key', keyVaultUrl: '${kv.properties.vaultUri}secrets/anthropic-api-key', identity: uami.id }
      ], telegramSecretRef)
    }
    template: {
      containers: [
        {
          name: 'telegram'
          image: image
          command: ['ifta']
          args: ['telegram-bot']
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
          env: [
            { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-api-key' }
            { name: 'TELEGRAM_BOT_TOKEN', secretRef: 'telegram-bot-token' }
            { name: 'TELEGRAM_ADMIN_USER_IDS', value: telegramAdminUserIds }
            { name: 'TELEGRAM_ADMIN_CHAT_ID', value: telegramAdminChatId }
            { name: 'TELEGRAM_AGENT_MODEL', value: agentModel }
            { name: 'TELEGRAM_AGENT_EFFORT', value: agentEffort }
            { name: 'IFTA_TELEGRAM_ACCESS_FILE', value: '/app/var/state/telegram_access.json' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          volumeMounts: [
            { volumeName: 'clients', mountPath: '/app/data/clients' }
            { volumeName: 'inbox', mountPath: '/app/inbox' }
            { volumeName: 'outputs', mountPath: '/app/outputs' }
            { volumeName: 'state', mountPath: '/app/var/state' }
          ]
        }
      ]
      volumes: [
        { name: 'clients', storageName: 'clients', storageType: 'AzureFile' }
        { name: 'inbox', storageName: 'inbox', storageType: 'AzureFile' }
        { name: 'outputs', storageName: 'outputs', storageType: 'AzureFile' }
        { name: 'state', storageName: 'state', storageType: 'AzureFile' }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// -------------------------- Cost guardrail (budget) --------------------------

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: '${namePrefix}-monthly-budget'
  properties: {
    category: 'Cost'
    amount: monthlyBudget
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStartDate }
    notifications: {
      actual_80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: [alertEmail]
      }
      forecast_100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Forecasted'
        contactEmails: [alertEmail]
      }
    }
  }
}

// -------------------------------- Outputs ------------------------------------

output webUrl string = deployContainerApps ? 'https://${webApp!.properties.configuration.ingress.fqdn}' : ''
output acrLoginServer string = acr.properties.loginServer
output acrName string = acr.name
output keyVaultName string = kv.name
output postgresFqdn string = pg.properties.fullyQualifiedDomainName
output managedIdentityClientId string = uami.properties.clientId
output managedIdentityPrincipalId string = uami.properties.principalId
output containerAppEnvName string = env.name
