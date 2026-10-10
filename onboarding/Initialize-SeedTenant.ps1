#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: Tenant-Onboarding, einmal pro Kunde (Tenant und Azure-DevOps-Projekt).

.DESCRIPTION
    Richtet alles ein, was Seed-Projekte in einem Tenant brauchen. Jeder Schritt prüft
    zuerst und legt nur an, was fehlt; das Skript lässt sich also gefahrlos wiederholen.

      1. Deployment-Identität: App-Registrierung mit Service Principal, ohne Secret
      2. Azure-Rechte auf der Subscription: Contributor und Role Based Access Control
         Administrator, per Bedingung auf die Storage-Datenrollen des core-Moduls beschränkt
      3. Microsoft Graph: Application.ReadWrite.OwnedBy mit Admin-Consent (Modul sso)
      4. Terraform-State: Resource Group, Storage Account ohne Shared Key, Container;
         Storage Blob Data Contributor nur auf dem Container
      5. Azure DevOps: Service Connection per Workload Identity Federation
      6. Azure DevOps: GitHub-Service-Connection für die Pipeline-Templates
      7. Azure DevOps: Repo und Pipeline seed-scaffold zum Anlegen neuer Projekte

    Ausführen mit einem Konto, das Owner der Subscription, Global Administrator (oder
    Privileged Role Administrator) im Tenant und Projektadministrator in Azure DevOps ist.

.PARAMETER DryRun
    Nur prüfen und anzeigen, was angelegt würde. Ändert nichts.

.EXAMPLE
    ./Initialize-SeedTenant.ps1 -SubscriptionId 0000... -AzureDevOpsOrganization https://dev.azure.com/kunde `
        -AzureDevOpsProject Apps -Approvers it@kunde.de -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $AzureDevOpsOrganization,
    [Parameter(Mandatory)] [string] $AzureDevOpsProject,
    [string] $Location = 'westeurope',
    [string] $IdentityName = 'sentinel-seed-deployment',
    [string] $ServiceConnectionName = 'sc-seed',
    [string] $GitHubConnectionName = 'github-blackforestsentinel',
    [string] $StateResourceGroup = 'rg-seed-tfstate',
    [string] $StateStorageAccount = '',
    [string] $StateContainer = 'tfstate',
    [string[]] $Approvers = @(),
    [string] $SeedPipelinesVersion = 'v0.3.0',
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$Org = $AzureDevOpsOrganization.TrimEnd('/')
$Subscription = "/subscriptions/$SubscriptionId"
$AzureDevOpsResource = '499b84ac-1321-427f-aa17-267ca6975798'
$GraphAppId = '00000003-0000-0000-c000-000000000000'
if (-not $StateStorageAccount) {
    # Global eindeutig und stabil je Subscription
    $hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($SubscriptionId))).ToLower()
    $StateStorageAccount = "stseedtf$($hash.Substring(0, 8))"
}
$StateContainerScope = "$Subscription/resourceGroups/$StateResourceGroup/providers/Microsoft.Storage/storageAccounts/$StateStorageAccount/blobServices/default/containers/$StateContainer"

# Rollen, die das core-Modul der Function-Identität zuweist
$StorageDataRoles = @(
    'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'   # Storage Blob Data Owner
    '974c5e8b-45b9-4653-ba55-5f855dd0fb88'   # Storage Queue Data Contributor
    '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'   # Storage Table Data Contributor
    'ba92f5b4-2d11-453d-a403-e96b0029c9fe'   # Storage Blob Data Contributor
)

$work = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) "seed-onboarding-$(Get-Random)")
$missing = [Collections.Generic.List[string]]::new()

function Save-Json([object] $Value, [string] $Name) {
    $path = Join-Path $work $Name
    $Value | ConvertTo-Json -Depth 20 | Set-Content -Path $path -Encoding utf8NoBOM
    $path
}

# Führt $Create nur aus, wenn $Exists nichts liefert; im DryRun wird nur gemeldet.
function Invoke-Step([string] $Title, [scriptblock] $Exists, [scriptblock] $Create) {
    $found = & $Exists
    if ($found) {
        Write-Host "  [ok]    $Title"
        return $found
    }
    if ($DryRun) {
        Write-Host "  [fehlt] $Title" -ForegroundColor Yellow
        $missing.Add($Title)
        return $null
    }
    $result = & $Create
    Write-Host "  [neu]   $Title" -ForegroundColor Green
    return $result
}

function Invoke-AzDevOps([string] $Method, [string] $Url, [object] $Body) {
    # Direkt per HTTP: URLs mit & überstehen den Umweg über az.cmd nicht.
    $token = az account get-access-token --resource $AzureDevOpsResource --query accessToken -o tsv
    $request = @{ Method = $Method; Uri = $Url; Headers = @{ Authorization = "Bearer $token" }; ContentType = 'application/json' }
    if ($null -ne $Body) {
        $request.Body = $Body | ConvertTo-Json -Depth 20
    }
    Invoke-RestMethod @request
}

# Für Prüfungen: Fehler (etwa 404 für eine fehlende Ressource) bedeuten "nicht vorhanden".
function Get-OrNull([scriptblock] $Command) {
    try { & $Command } catch { $null }
}

function Get-Endpoint([string] $Name) {
    az devops service-endpoint list --org $Org --project $AzureDevOpsProject -o json 2>$null |
        ConvertFrom-Json | Where-Object name -eq $Name
}

try {
    az account set --subscription $SubscriptionId
    $tenantId = az account show --query tenantId -o tsv
    $subscriptionName = az account show --query name -o tsv
    $project = az devops project show --org $Org --project $AzureDevOpsProject -o json | ConvertFrom-Json

    Write-Host "Sentinel Seed Onboarding$(if ($DryRun) { ' (DryRun, keine Änderungen)' })"
    Write-Host "  Tenant $tenantId, Subscription $subscriptionName, Azure DevOps $Org/$AzureDevOpsProject"
    Write-Host ''

    # --- 1. Deployment-Identität ----------------------------------------------------
    Write-Host '1. Deployment-Identität'
    $app = Invoke-Step "App-Registrierung $IdentityName" {
        az ad app list --display-name $IdentityName -o json | ConvertFrom-Json | Select-Object -First 1
    } {
        az ad app create --display-name $IdentityName --sign-in-audience AzureADMyOrg -o json | ConvertFrom-Json
    }
    $sp = if ($app) {
        Invoke-Step "Service Principal $IdentityName" {
            az ad sp list --filter "appId eq '$($app.appId)'" -o json | ConvertFrom-Json | Select-Object -First 1
        } {
            az ad sp create --id $app.appId -o json | ConvertFrom-Json
        }
    }

    # --- 2. Azure-Rechte --------------------------------------------------------------
    Write-Host '2. Azure-Rechte'
    $roleIds = $StorageDataRoles -join ', '
    $rbacCondition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR " +
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$roleIds})) AND " +
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR " +
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$roleIds}))"
    $assignments = @(
        @{ Scope = $Subscription; RoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'; Name = 'Contributor auf der Subscription' }
        @{ Scope = $Subscription; RoleId = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'; Name = 'Role Based Access Control Administrator (nur Storage-Datenrollen)'; Condition = $rbacCondition }
    )
    $existingAssignments = if ($sp) { az role assignment list --assignee $sp.id --all -o json | ConvertFrom-Json } else { @() }

    function Set-RoleAssignment([hashtable] $Assignment) {
        Invoke-Step $Assignment.Name {
            $existingAssignments | Where-Object { $_.scope -eq $Assignment.Scope -and $_.roleDefinitionId -like "*/$($Assignment.RoleId)" }
        } {
            $properties = @{
                roleDefinitionId = "$Subscription/providers/Microsoft.Authorization/roleDefinitions/$($Assignment.RoleId)"
                principalId      = $sp.id
                principalType    = 'ServicePrincipal'
                description      = "Sentinel Seed: $($Assignment.Name)"
            }
            if ($Assignment.Condition) {
                $properties.condition = $Assignment.Condition
                $properties.conditionVersion = '2.0'
            }
            $file = Save-Json @{ properties = $properties } "role-$(Get-Random).json"
            az rest --method put --url "https://management.azure.com$($Assignment.Scope)/providers/Microsoft.Authorization/roleAssignments/$([guid]::NewGuid())?api-version=2022-04-01" --body "@$file" -o none
            $true
        } | Out-Null
    }
    foreach ($a in $assignments) { Set-RoleAssignment $a }

    # --- 3. Microsoft Graph -------------------------------------------------------------
    Write-Host '3. Microsoft Graph'
    $graph = az ad sp show --id $GraphAppId -o json | ConvertFrom-Json
    $ownedBy = $graph.appRoles | Where-Object value -eq 'Application.ReadWrite.OwnedBy'
    Invoke-Step 'Application.ReadWrite.OwnedBy mit Admin-Consent' {
        if ($sp) {
            (az rest --method get --url "https://graph.microsoft.com/v1.0/servicePrincipals/$($sp.id)/appRoleAssignments" -o json | ConvertFrom-Json).value |
                Where-Object { $_.appRoleId -eq $ownedBy.id -and $_.resourceId -eq $graph.id }
        }
    } {
        $file = Save-Json @{ principalId = $sp.id; resourceId = $graph.id; appRoleId = $ownedBy.id } 'graph.json'
        az rest --method post --url "https://graph.microsoft.com/v1.0/servicePrincipals/$($sp.id)/appRoleAssignments" --body "@$file" -o none
        $true
    } | Out-Null

    # --- 4. Terraform-State ---------------------------------------------------------------
    Write-Host '4. Terraform-State'
    Invoke-Step "Resource Group $StateResourceGroup" {
        (az group exists --name $StateResourceGroup) -eq 'true'
    } {
        az group create --name $StateResourceGroup --location $Location --tags 'seed:purpose=terraform-state' -o none
        $true
    } | Out-Null
    Invoke-Step "Storage Account $StateStorageAccount (ohne Shared Key)" {
        Get-OrNull { az storage account show --name $StateStorageAccount --resource-group $StateResourceGroup --query id -o tsv 2>$null }
    } {
        az storage account create --name $StateStorageAccount --resource-group $StateResourceGroup --location $Location `
            --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false `
            --allow-shared-key-access false --tags 'seed:purpose=terraform-state' -o none
        $true
    } | Out-Null
    # Container über die Management-API: ohne Shared Key braucht das keine Datenrechte.
    $containerUrl = "https://management.azure.com$StateContainerScope`?api-version=2023-05-01"
    Invoke-Step "Container $StateContainer" {
        Get-OrNull { az rest --method get --url $containerUrl -o json 2>$null | ConvertFrom-Json }
    } {
        $file = Save-Json @{ properties = @{ publicAccess = 'None' } } 'container.json'
        az rest --method put --url $containerUrl --body "@$file" -o none
        $true
    } | Out-Null
    if ($sp) {
        Set-RoleAssignment @{ Scope = $StateContainerScope; RoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'; Name = "Storage Blob Data Contributor auf Container $StateContainer" }
    }

    # --- 5. Service Connection --------------------------------------------------------------
    Write-Host '5. Azure DevOps: Service Connection'
    $endpoint = Invoke-Step "Service Connection $ServiceConnectionName (Workload Identity Federation)" {
        Get-Endpoint $ServiceConnectionName
    } {
        $config = @{
            name          = $ServiceConnectionName
            type          = 'AzureRM'
            url           = 'https://management.azure.com/'
            description   = "Sentinel Seed: Deployment-Identität $IdentityName (Workload Identity Federation)"
            authorization = @{ scheme = 'WorkloadIdentityFederation'; parameters = @{ tenantid = $tenantId; serviceprincipalid = $app.appId } }
            data          = @{ environment = 'AzureCloud'; scopeLevel = 'Subscription'; subscriptionId = $SubscriptionId; subscriptionName = $subscriptionName; creationMode = 'Manual' }
            isShared      = $false
            isReady       = $true
            serviceEndpointProjectReferences = @(@{ projectReference = @{ id = $project.id; name = $project.name }; name = $ServiceConnectionName })
        }
        az devops service-endpoint create --org $Org --project $AzureDevOpsProject --service-endpoint-configuration (Save-Json $config 'endpoint.json') -o none 2>$null
        Get-Endpoint $ServiceConnectionName
    }
    if ($endpoint) {
        $issuer = $endpoint.authorization.parameters.workloadIdentityFederationIssuer
        $subject = $endpoint.authorization.parameters.workloadIdentityFederationSubject
        Invoke-Step 'Federated Credential an der App-Registrierung' {
            az ad app federated-credential list --id $app.appId -o json | ConvertFrom-Json | Where-Object subject -eq $subject
        } {
            $file = Save-Json @{
                name        = "ado-$($project.name -replace '[^A-Za-z0-9-]', '-')-$ServiceConnectionName"
                issuer      = $issuer
                subject     = $subject
                audiences   = @('api://AzureADTokenExchange')
                description = "Azure DevOps $Org / $AzureDevOpsProject / $ServiceConnectionName"
            } 'credential.json'
            az ad app federated-credential create --id $app.appId --parameters $file -o none
            $true
        } | Out-Null
    }

    # --- 6. GitHub-Service-Connection ------------------------------------------------------
    Write-Host '6. Azure DevOps: GitHub-Service-Connection'
    $github = Get-Endpoint $GitHubConnectionName
    if ($github) {
        Write-Host "  [ok]    GitHub-Service-Connection $GitHubConnectionName"
    } elseif ($env:SEED_GITHUB_TOKEN -and -not $DryRun) {
        $env:AZURE_DEVOPS_EXT_GITHUB_PAT = $env:SEED_GITHUB_TOKEN
        az devops service-endpoint github create --org $Org --project $AzureDevOpsProject `
            --github-url https://github.com/blackforestsentinel --name $GitHubConnectionName -o none
        Remove-Item Env:AZURE_DEVOPS_EXT_GITHUB_PAT
        $github = Get-Endpoint $GitHubConnectionName
        Write-Host "  [neu]   GitHub-Service-Connection $GitHubConnectionName" -ForegroundColor Green
    } else {
        Write-Host "  [fehlt] GitHub-Service-Connection $GitHubConnectionName" -ForegroundColor Yellow
        Write-Host '          Fine-grained Token mit Zugriff nur auf öffentliche Repos erzeugen und das Skript mit'
        Write-Host '          $env:SEED_GITHUB_TOKEN erneut starten, oder die Verbindung in Azure DevOps von Hand anlegen.'
        $missing.Add("GitHub-Service-Connection $GitHubConnectionName")
    }

    # --- 7. seed-scaffold -----------------------------------------------------------------
    Write-Host '7. Azure DevOps: seed-scaffold'
    $repo = Invoke-Step 'Repo seed-scaffold' {
        az repos list --org $Org --project $AzureDevOpsProject -o json | ConvertFrom-Json | Where-Object name -eq 'seed-scaffold'
    } {
        az repos create --org $Org --project $AzureDevOpsProject --name seed-scaffold -o json | ConvertFrom-Json
    }
    if ($repo) {
        Invoke-Step 'azure-pipelines.yml in seed-scaffold' {
            (Invoke-AzDevOps get "$Org/$($project.id)/_apis/git/repositories/$($repo.id)/refs?filter=heads/main&api-version=7.1" $null).count -gt 0
        } {
            $approverList = if ($Approvers.Count) { '[' + (($Approvers | ForEach-Object { "'$_'" }) -join ', ') + ']' } else { '[]' }
            $content = (Get-Content -Raw (Join-Path $PSScriptRoot '..' 'scaffold' 'azure-pipelines.yml')).
                Replace('__APPROVERS__', $approverList).
                Replace('__SEED_PIPELINES_VERSION__', $SeedPipelinesVersion).
                Replace('__GITHUB_CONNECTION__', $GitHubConnectionName).
                Replace('__SERVICE_CONNECTION__', $ServiceConnectionName).
                Replace('__STATE_RESOURCE_GROUP__', $StateResourceGroup).
                Replace('__STATE_STORAGE_ACCOUNT__', $StateStorageAccount).
                Replace('__STATE_CONTAINER__', $StateContainer)
            Invoke-AzDevOps post "$Org/$($project.id)/_apis/git/repositories/$($repo.id)/pushes?api-version=7.1" @{
                refUpdates = @(@{ name = 'refs/heads/main'; oldObjectId = '0000000000000000000000000000000000000000' })
                commits    = @(@{
                    comment = 'Sentinel Seed: seed-scaffold'
                    changes = @(@{ changeType = 'add'; item = @{ path = '/azure-pipelines.yml' }; newContent = @{ content = $content; contentType = 'rawtext' } })
                })
            } | Out-Null
            $true
        } | Out-Null
    }
    $pipeline = Invoke-Step 'Pipeline seed-scaffold' {
        az pipelines list --org $Org --project $AzureDevOpsProject --name seed-scaffold -o json | ConvertFrom-Json | Select-Object -First 1
    } {
        az pipelines create --org $Org --project $AzureDevOpsProject --name seed-scaffold --repository seed-scaffold `
            --repository-type tfsgit --branch main --yml-path azure-pipelines.yml --skip-first-run true -o json | ConvertFrom-Json
    }
    if ($pipeline -and $endpoint -and $github -and -not $DryRun) {
        foreach ($resource in @("endpoint/$($endpoint.id)", "endpoint/$($github.id)")) {
            Invoke-AzDevOps patch "$Org/$($project.id)/_apis/pipelines/pipelinePermissions/$($resource)?api-version=7.1-preview.1" @{
                pipelines = @(@{ id = $pipeline.id; authorized = $true })
            } | Out-Null
        }
        Write-Host '  [ok]    seed-scaffold für beide Service Connections berechtigt'
    }

    # --- Ergebnis ---------------------------------------------------------------------------
    Write-Host ''
    if ($missing.Count) {
        Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" }
    } else {
        Write-Host 'Fertig. Werte für azure-pipelines.yml eines Projekts:' -ForegroundColor Green
    }
    Write-Host "  serviceConnection: $ServiceConnectionName"
    Write-Host "  terraformState: resourceGroup $StateResourceGroup, storageAccount $StateStorageAccount, container $StateContainer"
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
