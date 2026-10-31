# Azure Storage Account & Container Setup Guide

This guide provides the exact step-by-step manual process and Azure CLI commands to create a secure, persistent Azure Blob Storage account in the **uaenorth** (UAE North) region under your **bingo-rg** resource group, and how to link it with your GitHub Actions deployment.

---

## 1. Create Azure Storage Account & Container

Run these Azure CLI commands in your terminal (make sure you are logged in using `az login` first):

### Step A: Define variables
Choose a globally unique storage account name (3-24 characters, numbers and lowercase letters only):
```bash
RESOURCE_GROUP="bingo-rg"
LOCATION="uaenorth"
STORAGE_ACCOUNT_NAME="melbetebingostor"  # REPLACE THIS with your desired unique name
CONTAINER_NAME="screenshots"
```

### Step B: Create the Storage Account
Create a general-purpose v2 storage account with Standard LRS (Locally Redundant Storage, Hot tier):
```bash
az storage account create \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --allow-blob-public-access false
```

### Step C: Retrieve the Connection String
Retrieve the connection string that the application will use to authenticate:
```bash
CONNECTION_STRING=$(az storage account show-connection-string \
  --name "$STORAGE_ACCOUNT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query connectionString \
  --output tsv)

echo "Your Connection String is:"
echo "$CONNECTION_STRING"
```
*Note: Keep this connection string secret and secure.*

### Step D: Create the Private Container
Create the `screenshots` container inside the storage account:
```bash
az storage container create \
  --name "$CONTAINER_NAME" \
  --connection-string "$CONNECTION_STRING"
```

---

## 2. Configure GitHub Secrets for Automated Deployment

The deployment pipeline is fully pre-configured to automatically inject these secrets into your container group. Follow these steps to register them in GitHub:

1. Open your repository on **GitHub**.
2. Go to **Settings** -> **Secrets and variables** -> **Actions**.
3. Scroll down to the **Environment secrets** section and select your **production** environment.
4. Click **Add secret** to add the following secret:
   - **Name:** `AZURE_STORAGE_CONNECTION_STRING`
   - **Value:** Paste the exact connection string retrieved in **Step C** above.
5. Click **Save secret**.

On the next automatic or manual push to the `main` branch, the GitHub Actions workflow will redeploy both the `bingo-web-api` and `bingo-bot` container groups, automatically mapping the `AZURE_STORAGE_CONNECTION_STRING` to the secure environment variables of both containers.

Now, all player-uploaded screenshots will be saved permanently in Azure Storage and will never be lost during container redeployments!
