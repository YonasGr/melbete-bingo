// Melbete Bingo - Azure infrastructure (resource-group scope).
//
// Creates: Log Analytics, Container Registry, managed identity, private Blob
// storage (deposit screenshots), optional Azure Database for PostgreSQL
// Flexible Server, a Container Apps environment, and two apps from ONE image:
//   - api: FastAPI + WebSocket + Mini App frontend (public HTTPS ingress)
//   - bot: Telegram long-polling bot (no ingress)
//
// The bot is pinned to ONE replica (long polling). The api defaults to one
// replica too: draw loops and lobby timers are protected by database leases,
// but WebSocket messages only reach every container when REDIS_URL is set.
// Raise apiMaxReplicas only after the checklist in DEPLOY_AZURE.md.
//
// Two-phase deploy (scripts/deploy_azure.sh does this for you):
//   1) deployApps=false -> registry, storage, database, environment
//   2) build + push the image, then deployApps=true -> the two apps

targetScope = 'resourceGroup'

@minLength(3)
@maxLength(12)
@description('Lowercase prefix for resource names.')
param namePrefix string = 'melbetebingo'

param location string = resourceGroup().location

@description('Phase switch: false creates shared infra only, true also creates the api and bot apps.')
param deployApps bool = false

@description('Image tag in the registry (repository bingo-app).')
param imageTag string = 'latest'

// ---- Database ---------------------------------------------------------------
@description('true = create Azure Database for PostgreSQL. false = use externalDatabaseUrl (e.g. Supabase).')
param createPostgres bool = true

@secure()
@description('Letters and digits only (it is embedded in a URL). Required when createPostgres is true.')
param postgresAdminPassword string = ''

@secure()
@description('Full connection string when createPostgres is false.')
param externalDatabaseUrl string = ''

param postgresSkuName string = 'Standard_B1ms'
param postgresSkuTier string = 'Burstable'
param postgresStorageGB int = 32

// ---- App settings -------------------------------------------------------------
@secure()
@description('Number of API containers. Keep 1 until REDIS_URL is set and the checklist in DEPLOY_AZURE.md ("More than one API container") is done. The bot is always 1.')
@minValue(1)
@maxValue(5)
param apiMaxReplicas int = 1

@description('Optional Redis connection string (rediss://:key@host:6380/0) for sharing live-game messages between API containers.')
@secure()
param redisUrl string = ''

param telegramBotToken string

@secure()
@description('Long random string, signs admin sessions.')
param secretKey string

param telegramBotUsername string = ''
param superAdminTelegramId string = ''

@secure()
@description('TELEGRAM_ID:BASE32SEED,... Admin login fails closed if empty.')
param adminTotpSecrets string = ''

param adminTelegramIds string = ''
param adminChatId string = ''

@description('Public URL override (custom domain). Empty = the generated *.azurecontainerapps.io URL.')
param publicBaseUrl string = ''

param gameStakeAmount string = '10'
param freeCreditAmount string = '50'
param lobbyCountdownSeconds string = '30'
param maxPlayersPerRoom string = '100'
param paymentMethods string = 'Telebirr,CBE Birr'

// ---- Names ----------------------------------------------------------------------
var uniq = uniqueString(resourceGroup().id)
var acrName = take('${namePrefix}${uniq}', 50)
var storageName = take(toLower('${namePrefix}${uniq}'), 24)
var pgName = '${namePrefix}-pg-${take(uniq, 6)}'
var pgHost = '${pgName}.postgres.database.azure.com'
var apiAppName = '${namePrefix}-api'
var botAppName = '${namePrefix}-bot'
var imageName = '${acr.properties.loginServer}/bingo-app:${imageTag}'

// ---- Shared infrastructure --------------------------------------------------------
resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${namePrefix}-id'
  location: location
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, identity.id, 'acrpull')
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource screenshots 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'screenshots'
  properties: { publicAccess: 'None' }
}

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = if (createPostgres) {
  name: pgName
  location: location
  sku: {
    name: postgresSkuName
    tier: postgresSkuTier
  }
  properties: {
    version: '16'
    administratorLogin: 'bingoadmin'
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: postgresStorageGB }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
  }
}

resource pgDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = if (createPostgres) {
  parent: pg
  name: 'bingo'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// 0.0.0.0 -> 0.0.0.0 is Azure's "allow Azure services" rule (needed for Container Apps).
resource pgAllowAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-06-01-preview' = if (createPostgres) {
  parent: pg
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${namePrefix}-env'
  location: location
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

// ---- Derived settings -----------------------------------------------------------------
var baseUrl = empty(publicBaseUrl) ? 'https://${apiAppName}.${env.properties.defaultDomain}' : publicBaseUrl
var databaseUrl = createPostgres ? 'postgresql://bingoadmin:${postgresAdminPassword}@${pgHost}:5432/bingo?sslmode=require' : externalDatabaseUrl
var storageConnection = 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

var secrets = concat([
  { name: 'telegram-bot-token', value: telegramBotToken }
  { name: 'secret-key', value: secretKey }
  { name: 'database-url', value: databaseUrl }
  { name: 'storage-connection', value: storageConnection }
], empty(adminTotpSecrets) ? [] : [
  { name: 'admin-totp-secrets', value: adminTotpSecrets }
], empty(redisUrl) ? [] : [
  { name: 'redis-url', value: redisUrl }
])

var secretEnv = concat([
  { name: 'TELEGRAM_BOT_TOKEN', secretRef: 'telegram-bot-token' }
  { name: 'SECRET_KEY', secretRef: 'secret-key' }
  { name: 'DATABASE_URL', secretRef: 'database-url' }
  { name: 'AZURE_STORAGE_CONNECTION_STRING', secretRef: 'storage-connection' }
], empty(adminTotpSecrets) ? [] : [
  { name: 'ADMIN_TOTP_SECRETS', secretRef: 'admin-totp-secrets' }
], empty(redisUrl) ? [] : [
  { name: 'REDIS_URL', secretRef: 'redis-url' }
])

var plainEnv = [
  { name: 'APP_ENV', value: 'production' }
  { name: 'DEBUG', value: 'False' }
  { name: 'HOST', value: '0.0.0.0' }
  { name: 'PORT', value: '8000' }
  // Container Apps terminates TLS and forwards X-Forwarded-*; trust it.
  { name: 'FORWARDED_ALLOW_IPS', value: '*' }
  { name: 'PUBLIC_BASE_URL', value: baseUrl }
  { name: 'TELEGRAM_BOT_USERNAME', value: telegramBotUsername }
  { name: 'TELEGRAM_GAME_SHORT_NAME', value: 'bingo' }
  { name: 'AZURE_STORAGE_CONTAINER_NAME', value: 'screenshots' }
  // Direct Postgres connection (no PgBouncer) -> prepared statements are safe.
  { name: 'DB_USE_PREPARED_STATEMENTS', value: string(createPostgres) }
  { name: 'DB_POOL_MIN_SIZE', value: '2' }
  { name: 'DB_POOL_MAX_SIZE', value: '20' }
  { name: 'DB_COMMAND_TIMEOUT_SECONDS', value: '15' }
  { name: 'SUPER_ADMIN_TELEGRAM_ID', value: superAdminTelegramId }
  { name: 'ADMIN_TELEGRAM_IDS', value: adminTelegramIds }
  { name: 'ADMIN_CHAT_ID', value: adminChatId }
  { name: 'PAYMENT_METHODS', value: paymentMethods }
  { name: 'MAX_PLAYERS_PER_ROOM', value: maxPlayersPerRoom }
  { name: 'GAME_STAKE_AMOUNT', value: gameStakeAmount }
  { name: 'FREE_CREDIT_AMOUNT', value: freeCreditAmount }
  { name: 'LOBBY_COUNTDOWN_SECONDS', value: lobbyCountdownSeconds }
  { name: 'DEFAULT_DRAW_INTERVAL', value: '2' }
  { name: 'AUTO_MARK_ENABLED', value: 'False' }
  { name: 'MIN_PLAYERS_TO_START', value: '1' }
  { name: 'MIN_DEPOSIT_AMOUNT', value: '50' }
  { name: 'MAX_DEPOSIT_AMOUNT', value: '70000' }
  { name: 'MIN_WITHDRAWAL_AMOUNT', value: '200' }
  { name: 'MAX_WITHDRAWAL_AMOUNT', value: '10000' }
]

// Container Apps rejects env entries with an empty value, so drop them.
var appEnv = concat(secretEnv, filter(plainEnv, e => !empty(e.value)))

var registryConfig = [
  {
    server: acr.properties.loginServer
    identity: identity.id
  }
]

var identityConfig = {
  type: 'UserAssigned'
  userAssignedIdentities: { '${identity.id}': {} }
}

var healthProbes = [
  {
    type: 'Startup'
    httpGet: { path: '/ready', port: 8000 }
    initialDelaySeconds: 5
    periodSeconds: 5
    failureThreshold: 24
  }
  {
    type: 'Liveness'
    httpGet: { path: '/health', port: 8000 }
    periodSeconds: 30
    failureThreshold: 3
  }
  {
    type: 'Readiness'
    httpGet: { path: '/ready', port: 8000 }
    periodSeconds: 10
    failureThreshold: 3
  }
]

// ---- Apps (phase 2) ---------------------------------------------------------------------
resource api 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: apiAppName
  location: location
  identity: identityConfig
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
      registries: registryConfig
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'api'
          image: imageName
          command: [ 'python', 'main.py', 'api' ]
          env: appEnv
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: healthProbes
        }
      ]
      scale: { minReplicas: 1, maxReplicas: apiMaxReplicas }
    }
  }
  dependsOn: [ acrPull, screenshots, pgDb, pgAllowAzure ]
}

resource bot 'Microsoft.App/containerApps@2024-03-01' = if (deployApps) {
  name: botAppName
  location: location
  identity: identityConfig
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: registryConfig
      secrets: secrets
    }
    template: {
      containers: [
        {
          name: 'bot'
          image: imageName
          command: [ 'python', 'main.py', 'bot' ]
          env: appEnv
          resources: { cpu: json('0.25'), memory: '0.5Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [ acrPull, screenshots, pgDb, pgAllowAzure ]
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output apiAppName string = apiAppName
output botAppName string = botAppName
output publicBaseUrl string = baseUrl
output postgresServerName string = createPostgres ? pgName : ''
output postgresHost string = createPostgres ? pgHost : ''
