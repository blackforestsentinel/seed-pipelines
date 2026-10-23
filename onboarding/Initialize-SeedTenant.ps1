#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: Tenant-Onboarding, einmal je Subscription und Azure-DevOps-Projekt.

.DESCRIPTION
    Richtet alles ein, was Seed-Projekte in einer Subscription brauchen. Jeder Schritt prüft
    zuerst und legt nur an, was fehlt; das Skript lässt sich also gefahrlos wiederholen.

      1. Deployment-Identität: App-Registrierung mit Service Principal, ohne Secret
      2. Azure-Rechte auf der Subscription: Contributor und Role Based Access Control
         Administrator, per Bedingung auf die Rollen beschränkt, die Seed-Module vergeben
         (Storage-Daten, Key-Vault-Secrets, Monitoring Metrics Publisher); eine bestehende
         Zuweisung mit älterer Bedingung wird aktualisiert; dazu die eigene Rolle
         "Sentinel Seed Lock Contributor" nur für Löschsperren (Modul storage)
      3. Microsoft Graph: Application.ReadWrite.OwnedBy mit Admin-Consent (Modul sso)
      4. Terraform-State: Resource Group, Storage Account ohne Shared Key, Container;
         Storage Blob Data Contributor nur auf dem Container
      5. Azure DevOps: Service Connection per Workload Identity Federation, mit Branch-Prüfung:
         nur Läufe von -AllowedBranches (Standard main) bekommen die Identität (nicht mit
         -SkipScaffold, dort setzt sie Initialize-SeedProject.ps1 -EnableBranchPolicy)
      6. Azure DevOps: GitHub-Service-Connection für die Pipeline-Templates
      7. Azure DevOps: Repo und Pipeline seed-scaffold zum Anlegen neuer Projekte
         (entfällt mit -SkipScaffold). Mit -CentralScaffold als zentrales Scaffold für andere
         Azure-DevOps-Projekte, ohne PAT: eigene Managed Identity nur für den State (Storage Blob
         Data Contributor auf dem Container, Reader auf der Subscription) mit Service Connection,
         dazu für die Build-Service-Identität dieses Projekts die Rolle Administrator auf
         Organisationsebene an den Service Connections aus 5 und 6, damit sie sie teilen kann
      8. Azure DevOps: GitHub-Service-Connection für alle Pipelines
      9. Power Platform (nur mit -PowerPlatformEnvironmentUrl): die Deployment-Identität als
         Anwendungsbenutzer mit der Rolle Environment Maker in jeder genannten Umgebung, damit
         die Pipeline Custom Connectors anlegen, aktualisieren und teilen kann

    Weitere Subscription im selben Tenant: Skript erneut ausführen, mit eigener -IdentityName
    und eigener -ServiceConnectionName. Die Rolle "Sentinel Seed Lock Contributor" ist im
    Tenant eindeutig benannt; das Skript sucht sie in allen sichtbaren Subscriptions und
    ergänzt die neue Subscription in ihren assignableScopes. Andere Identitäten fasst es nicht an.

    Danach einmalig (nicht mit -SkipScaffold oder -CentralScaffold): PAT für seed-scaffold anlegen und als geheime
    Variable SeedScaffoldPat an der Pipeline seed-scaffold hinterlegen (Scopes: Code
    read/write/manage, Build read/execute, Environment read/manage, Graph read, Project and
    Team read, Pipeline Resources use/manage). Das Job-Token der Pipeline darf keine Repos
    anlegen, solange "Protect access to repositories in YAML pipelines" an ist. Die Person
    hinter dem PAT braucht "Edit policies" an den Repos (Branch-Policy der PR-Validierung).
    Die Azure-Service-Connection gibt beim ersten Lauf jedes neuen Projekts eine
    Administratorin oder ein Administrator frei.

    Ausführen mit einem Konto, das Owner der Subscription, Global Administrator (oder
    Privileged Role Administrator) im Tenant und Projektadministrator in Azure DevOps ist; mit
    -PowerPlatformEnvironmentUrl zusätzlich System Administrator in diesen Umgebungen.
    Muss die Rolle für Löschsperren um eine Subscription erweitert werden, braucht das Konto
    auch auf den Subscriptions, in denen sie schon zuweisbar ist, das Recht, Rollendefinitionen
    zu schreiben (Owner oder User Access Administrator).

.PARAMETER IdentityName
    Name der App-Registrierung. Je Subscription eine eigene Identität; ein Name, den es im
    Tenant schon gibt, meint diese Identität.

.PARAMETER SkipScaffold
    Repo und Pipeline seed-scaffold weglassen: für Projekte, die migriert statt neu angelegt
    werden. Das bestehende Repo richtet danach onboarding/Initialize-SeedProject.ps1 ein.

.PARAMETER AllowedBranches
    Branches, deren Läufe die Azure-Service-Connection nutzen dürfen, Standard main. Ohne diese
    Prüfung könnte jede Pipeline-Datei aus einem Feature-Branch die Deployment-Identität nutzen,
    an allen Freigaben vorbei; die hängen an den Environments. Muster wie refs/heads/release/*
    sind erlaubt. Ein erneuter Lauf mit anderen Branches aktualisiert die Prüfung.

.PARAMETER TemplateRefs
    Refs von seed-pipelines, die die Branch-Prüfung zusätzlich erlaubt, Standard refs/tags/v*.
    Die Prüfung vergleicht jedes Repo des Laufs mit derselben Liste, auch das Template-Repo, das
    Projekte über einen Tag einbinden. Ein Tag v… in einem Projekt-Repo und ein Lauf darauf kämen
    deshalb ebenfalls durch: In den Repos des Azure-DevOps-Projekts sollten nur Administratoren
    Tags anlegen dürfen (README, Branch-Prüfung).

.PARAMETER Pool
    Agent-Pool für seed-scaffold, etwa Default für eigene Agents; leer: Microsoft-gehostete
    Agents. Landet in azure-pipelines.yml im Repo seed-scaffold (README, Eigene Agents).

.PARAMETER CentralScaffold
    seed-scaffold als zentrales Scaffold (README, Zentrales Scaffold): Es legt Seed-Projekte auch
    in anderen Azure-DevOps-Projekten an, mit dem Job-Token statt eines PATs. Je Zielprojekt
    schaltet ein Projektadmin es einmal frei (Grant-SeedScaffoldAccess.ps1). Nicht zusammen mit
    -SkipScaffold.

.PARAMETER AllowedProjects
    Nur mit -CentralScaffold: regulärer Ausdruck für erlaubte Zielprojekte, leer: alle
    freigeschalteten.

.PARAMETER MemberGroup
    Nur mit -CentralScaffold: Gruppe im Zielprojekt, in der die startende Person Mitglied sein
    muss, Standard Contributors.

.PARAMETER ApproverGroups
    Nur mit -CentralScaffold: Gruppen der Organisation, deren Mitglieder Infrastruktur-Änderungen
    jedes Projekts freigeben. Mit Gruppe darf die auslösende Person nicht selbst freigeben.

.PARAMETER MinReviewers
    Nur mit -CentralScaffold: Pflicht-Reviewer für Pull Requests auf main, Standard 1.

.PARAMETER ProjectPool
    Nur mit -CentralScaffold: Agent-Pool, den neue Projekte in azure-pipelines.yml bekommen.

.PARAMETER PowerPlatformEnvironmentUrl
    Umgebungs-URL (Dataverse) jeder Power-Platform-Umgebung, in der Seed-Projekte mit
    features.customConnector einen Connector anlegen, z. B. https://org0000000.crm4.dynamics.com
    (Power Platform Admin Center, Umgebung, „Umgebungs-URL“). Die Umgebung braucht Dataverse.
    Weitere Umgebungen später: Skript erneut mit allen gewünschten URLs ausführen.

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
    [string] $SeedPipelinesVersion = 'v0.14.0',
    [string[]] $PowerPlatformEnvironmentUrl = @(),
    [string[]] $AllowedBranches = @('main'),
    [string[]] $TemplateRefs = @('refs/tags/v*'),
    [string] $Pool = '',
    [switch] $SkipScaffold,
    [switch] $CentralScaffold,
    [string] $ScaffoldIdentityName = 'id-seed-scaffold',
    [string] $ScaffoldConnectionName = 'sc-seed-scaffold',
    [string] $AllowedProjects = '',
    [string] $MemberGroup = 'Contributors',
    [string[]] $ApproverGroups = @(),
    [ValidateRange(0, 10)] [int] $MinReviewers = 1,
    [string] $ProjectPool = '',
    [switch] $DryRun
)

if ($CentralScaffold -and $SkipScaffold) {
    throw '-CentralScaffold und -SkipScaffold schließen sich aus.'
}

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

# Rollen, die Seed-Module vergeben dürfen: der Function-Identität (core, storage, keyvault,
# monitoring), der Pipeline-Identität auf dem eigenen Key Vault und Personen, die Secrets setzen.
# Andere Rollen, etwa Owner oder User Access Administrator, kann die Pipeline nicht vergeben.
$AssignableRoles = @(
    'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'   # Storage Blob Data Owner
    '974c5e8b-45b9-4653-ba55-5f855dd0fb88'   # Storage Queue Data Contributor
    '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'   # Storage Table Data Contributor
    'ba92f5b4-2d11-453d-a403-e96b0029c9fe'   # Storage Blob Data Contributor
    '4633458b-17de-408a-b874-0445c86b69e6'   # Key Vault Secrets User
    'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'   # Key Vault Secrets Officer
    '3913510d-42f4-4e42-8a64-420c390055eb'   # Monitoring Metrics Publisher
)

# Eigene Rolle für Löschsperren: Contributor darf keine Sperren setzen, Owner oder User Access
# Administrator wären viel zu weit. Diese Rolle darf nur Sperren lesen, setzen und entfernen.
$LockRoleName = 'Sentinel Seed Lock Contributor'

$work = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) "seed-onboarding-$(Get-Random)")
$missing = [Collections.Generic.List[string]]::new()

function Save-Json([object] $Value, [string] $Name) {
    $path = Join-Path $work $Name
    ConvertTo-Json -InputObject $Value -Depth 20 | Set-Content -Path $path -Encoding utf8NoBOM
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
        # -InputObject: ein Array mit einem Eintrag bleibt ein Array
        $request.Body = ConvertTo-Json -InputObject $Body -Depth 20
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

# YAML-Liste in einfachen Anführungszeichen, etwa für Freigebende in azure-pipelines.yml
function ConvertTo-YamlList([string[]] $Values) {
    '[' + (($Values | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }) -join ', ') + ']'
}

# Build-Service-Identität eines Projekts über die Graph-API (Dienstidentitäten, subjectType svc).
# Sie entsteht mit dem Projekt; geliefert werden Deskriptor und Identitäts-ID.
function Find-BuildIdentity([string] $DisplayName) {
    $vssps = "https://vssps.dev.azure.com/$($Org.Split('/')[-1])"
    $continuation = $null
    do {
        $url = "$vssps/_apis/graph/users?subjectTypes=svc&api-version=7.1-preview.1"
        if ($continuation) { $url += "&continuationToken=$([uri]::EscapeDataString($continuation))" }
        $token = az account get-access-token --resource $AzureDevOpsResource --query accessToken -o tsv
        $response = Invoke-RestMethod -Uri $url -Headers @{ Authorization = "Bearer $token" } -ResponseHeadersVariable headers
        $match = $response.value | Where-Object displayName -eq $DisplayName | Select-Object -First 1
        if ($match) {
            $id = (Invoke-AzDevOps get "$vssps/_apis/graph/storagekeys/$($match.descriptor)?api-version=7.1-preview.1" $null).value
            return [pscustomobject] @{ Descriptor = $match.descriptor; Id = $id }
        }
        $continuation = if ($headers['x-ms-continuationtoken']) { @($headers['x-ms-continuationtoken'])[0] }
    } while ($continuation)
}

# Branch-Prüfung (Check „Branch control“) an einer Service Connection: Läufe anderer Branches
# bekommen sie nicht, auch keine PR-Läufe. Freigaben hängen an den Environments und hielten
# eine geänderte Pipeline-Datei auf einem Feature-Branch nicht auf, die die Service Connection
# direkt nutzt. Die Prüfung gilt für jedes Repo des Laufs, auch für seed-pipelines; deshalb
# stehen die Template-Refs mit in der Liste. Eine vorhandene Prüfung mit anderen Branches wird
# aktualisiert.
$BranchControlCheckType = 'fe1de3ee-a436-41b4-bb20-f6eb4cb879a7'
$BranchControlDefinition = '86b05a0c-73e6-4f7d-b3cf-e38f3b39a75b'
$AllowedBranchRefs = @(@($AllowedBranches | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ } |
            ForEach-Object { if ($_ -like 'refs/*') { $_ } else { "refs/heads/$_" } }) +
    @($TemplateRefs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })) -join ','

function Set-BranchControl([object] $Endpoint, [string] $ProjectId) {
    $checks = "$Org/$ProjectId/_apis/pipelines/checks/configurations"
    $title = "Branch-Prüfung an $($Endpoint.name): $AllowedBranchRefs"
    $existing = (Invoke-AzDevOps get "$checks`?resourceType=endpoint&resourceId=$($Endpoint.id)&`$expand=settings&api-version=7.1-preview.1" $null).value |
        Where-Object { $_.type.id -eq $BranchControlCheckType -and $_.settings.definitionRef.id -eq $BranchControlDefinition } |
        Select-Object -First 1
    $body = @{
        type     = @{ id = $BranchControlCheckType; name = 'Task Check' }
        resource = @{ type = 'endpoint'; id = "$($Endpoint.id)"; name = $Endpoint.name }
        settings = @{
            definitionRef = @{ id = $BranchControlDefinition; name = 'evaluatebranchProtection'; version = '0.0.1' }
            displayName   = 'Sentinel Seed: nur freigegebene Branches'
            inputs        = @{ allowedBranches = $AllowedBranchRefs; ensureProtectionOfBranch = 'false'; allowUnknownStatusBranch = 'false' }
        }
        timeout  = 1440
    }
    if ($existing -and $existing.settings.inputs.allowedBranches -ne $AllowedBranchRefs) {
        if ($DryRun) {
            Write-Host "  [alt]   $title (bisher $($existing.settings.inputs.allowedBranches))" -ForegroundColor Yellow
            $missing.Add($title)
            return
        }
        $body.id = $existing.id
        Invoke-AzDevOps patch "$checks/$($existing.id)?api-version=7.1-preview.1" $body | Out-Null
        Write-Host "  [neu]   $title (bisher $($existing.settings.inputs.allowedBranches))" -ForegroundColor Green
        return
    }
    Invoke-Step $title { $existing } {
        Invoke-AzDevOps post "$checks`?api-version=7.1-preview.1" $body
    } | Out-Null
}

function Invoke-Arm([string] $Method, [string] $Url, [string] $BodyFile) {
    # Direkt per HTTP wie bei Azure DevOps: Filter-URLs mit & überstehen az.cmd nicht, und az gibt
    # Umlaute unter Windows als Ersatzzeichen aus. Gelesene Texte gehen hier zurück an Azure,
    # der Body deshalb unverändert als UTF-8-Datei.
    $token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
    $request = @{ Method = $Method; Uri = $Url; Headers = @{ Authorization = "Bearer $token" } }
    if ($BodyFile) {
        $request.InFile = $BodyFile
        $request.ContentType = 'application/json'
    }
    Invoke-RestMethod @request
}

# Eigene Rollen sind im Tenant eindeutig benannt, sichtbar aber nur unter ihren assignableScopes.
# Deshalb in allen Subscriptions des Tenants suchen, die das Konto sieht, die Ziel-Subscription zuerst.
function Find-CustomRole([string] $RoleName) {
    $subscriptions = [Collections.Generic.List[string]]::new()
    $subscriptions.Add($SubscriptionId)
    $url = 'https://management.azure.com/subscriptions?api-version=2022-12-01'
    while ($url) {
        $page = Invoke-Arm get $url
        $page.value | Where-Object { $_.tenantId -eq $tenantId -and $_.state -eq 'Enabled' -and $_.subscriptionId -ne $SubscriptionId } |
            ForEach-Object { $subscriptions.Add($_.subscriptionId) }
        $url = $page.nextLink
    }
    $filter = [uri]::EscapeDataString("roleName eq '$RoleName'")
    foreach ($id in $subscriptions) {
        $role = (Invoke-Arm get "https://management.azure.com/subscriptions/$id/providers/Microsoft.Authorization/roleDefinitions?api-version=2022-04-01&`$filter=$filter").value |
            Where-Object { $_.properties.type -eq 'CustomRole' } | Select-Object -First 1
        if ($role) { return $role }
    }
}

# Ein falsches Token (etwa ein kopierter Platzhalter) fiele sonst erst beim ersten Pipeline-Lauf
# auf, wenn die Verbindung schon existiert und Schritt 6 sie nicht mehr anfasst. Deshalb vorher
# gegen GitHub prüfen, bevor irgendetwas angelegt wird.
if ($env:SEED_GITHUB_TOKEN) {
    try {
        $null = Invoke-RestMethod -Uri 'https://api.github.com/repos/blackforestsentinel/seed-pipelines' `
            -Headers @{ Authorization = "Bearer $env:SEED_GITHUB_TOKEN"; 'User-Agent' = 'sentinel-seed-onboarding' }
    } catch {
        throw '$env:SEED_GITHUB_TOKEN wird von GitHub nicht angenommen. Ein echtes Fine-grained Token mit Lesezugriff auf öffentliche Repos eintragen, nicht den Platzhalter aus der Anleitung.'
    }
}

try {
    # Das Skript wechselt die Subscription von az; am Ende gilt wieder die vorherige.
    $previousSubscription = Get-OrNull { az account show --query id -o tsv 2>$null }
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
        # --display-name sucht nach dem Anfang des Namens: "sentinel-seed" fände auch
        # "sentinel-seed-deployment". Deshalb nur exakte Treffer, und nur einer.
        $apps = @(az ad app list --display-name $IdentityName -o json | ConvertFrom-Json | Where-Object displayName -eq $IdentityName)
        if ($apps.Count -gt 1) { throw "Es gibt $($apps.Count) App-Registrierungen mit dem Namen $IdentityName; der Name muss im Tenant eindeutig sein." }
        $apps | Select-Object -First 1
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
    $roleIds = $AssignableRoles -join ', '
    $rbacCondition = "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR " +
        "(@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$roleIds})) AND " +
        "((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR " +
        "(@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {$roleIds}))"
    $assignments = @(
        @{ Scope = $Subscription; RoleId = 'b24988ac-6180-42a0-ab88-20f7382dd24c'; Name = 'Contributor auf der Subscription' }
        @{ Scope = $Subscription; RoleId = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'; Name = 'Role Based Access Control Administrator (nur Seed-Rollen)'; Condition = $rbacCondition }
    )
    $existingAssignments = if ($sp) { az role assignment list --assignee $sp.id --all -o json | ConvertFrom-Json } else { @() }

    function Set-RoleAssignment([hashtable] $Assignment) {
        $existing = $existingAssignments | Where-Object { $_.scope -eq $Assignment.Scope -and $_.roleDefinitionId -like "*/$($Assignment.RoleId)" } |
            Select-Object -First 1
        # Neue Seed-Versionen erlauben weitere Rollen: eine ältere Bedingung wird ersetzt, nicht ergänzt.
        if ($existing -and $Assignment.Condition -and $existing.condition -ne $Assignment.Condition) {
            if ($DryRun) {
                Write-Host "  [alt]   $($Assignment.Name): Bedingung wird aktualisiert" -ForegroundColor Yellow
                $missing.Add("$($Assignment.Name) (Bedingung)")
                return
            }
            $file = Save-Json @{ properties = @{
                    roleDefinitionId = $existing.roleDefinitionId
                    principalId      = $existing.principalId
                    principalType    = 'ServicePrincipal'
                    description      = "Sentinel Seed: $($Assignment.Name)"
                    condition        = $Assignment.Condition
                    conditionVersion = '2.0'
                } } "role-$(Get-Random).json"
            az rest --method put --url "https://management.azure.com$($existing.id)?api-version=2022-04-01" --body "@$file" -o none
            Write-Host "  [neu]   $($Assignment.Name): Bedingung aktualisiert" -ForegroundColor Green
            return
        }
        Invoke-Step $Assignment.Name { $existing } {
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

    # Die Rolle gibt es im Tenant nur einmal. Ist sie schon für eine andere Subscription angelegt,
    # kommt diese Subscription in ihre assignableScopes; neu angelegt wird sie nur, wenn sie fehlt.
    $lockRole = Find-CustomRole $LockRoleName
    $lockScopes = if ($lockRole) { @($lockRole.properties.assignableScopes) } else { @() }
    if ($lockRole -and -not ($lockScopes | Where-Object { $_ -eq $Subscription -or $_ -eq '/' })) {
        if ($DryRun) {
            Write-Host "  [alt]   Rolle ${LockRoleName}: Subscription wird in assignableScopes ergänzt" -ForegroundColor Yellow
            $missing.Add("Rolle $LockRoleName (assignableScopes)")
        } else {
            # Definition unverändert zurückschreiben, nur mit der zusätzlichen Subscription.
            $definition = $lockRole.properties
            $file = Save-Json @{ properties = @{
                    roleName         = $definition.roleName
                    description      = $definition.description
                    type             = $definition.type
                    permissions      = $definition.permissions
                    assignableScopes = @($lockScopes) + $Subscription
                } } 'lock-role-scopes.json'
            Invoke-Arm put "https://management.azure.com$($lockRole.id)?api-version=2022-04-01" $file | Out-Null
            Write-Host "  [neu]   Rolle ${LockRoleName}: Subscription in assignableScopes ergänzt" -ForegroundColor Green
        }
    } else {
        $lockRole = Invoke-Step "Rolle $LockRoleName" { $lockRole } {
            $file = Save-Json @{
                Name             = $LockRoleName
                Description      = 'Sentinel Seed: Löschsperren lesen, setzen und entfernen (Modul storage).'
                Actions          = @('Microsoft.Authorization/locks/read', 'Microsoft.Authorization/locks/write', 'Microsoft.Authorization/locks/delete')
                NotActions       = @()
                AssignableScopes = @($Subscription)
            } 'lock-role.json'
            try {
                az role definition create --role-definition "@$file" -o json | ConvertFrom-Json
            } catch {
                throw "Rolle $LockRoleName ließ sich nicht anlegen. Gibt es sie schon unter einer Subscription, die dieses Konto nicht sieht? Dann dort Leserechte geben und neu starten. ($_)"
            }
        }
    }
    if ($lockRole) {
        # Eine neue oder erweiterte Rollendefinition kennt Azure nicht sofort überall; deshalb
        # einige Versuche, zusammen bis zu zwei Minuten.
        foreach ($attempt in 1..12) {
            try {
                Set-RoleAssignment @{ Scope = $Subscription; RoleId = $lockRole.name; Name = "$LockRoleName auf der Subscription" }
                break
            } catch {
                if ($attempt -eq 12) { throw }
                Write-Host '          Rollendefinition noch nicht verteilt, neuer Versuch in 10 Sekunden'
                Start-Sleep -Seconds 10
            }
        }
    }

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
    # Ohne Service Principal (nur im DryRun) meldet das den Schritt als fehlend.
    Set-RoleAssignment @{ Scope = $StateContainerScope; RoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'; Name = "Storage Blob Data Contributor auf Container $StateContainer" }

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
                # Eindeutig je App-Registrierung; erlaubt sind nur Buchstaben, Ziffern, - und _.
                name        = "ado-$($project.name)-$ServiceConnectionName" -replace '[^A-Za-z0-9_-]', '-'
                issuer      = $issuer
                subject     = $subject
                audiences   = @('api://AzureADTokenExchange')
                description = "Azure DevOps $Org / $AzureDevOpsProject / $ServiceConnectionName"
            } 'credential.json'
            az ad app federated-credential create --id $app.appId --parameters $file -o none
            $true
        } | Out-Null
    }
    if ($SkipScaffold) {
        Write-Host '          Branch-Prüfung folgt mit Initialize-SeedProject.ps1 -EnableBranchPolicy: bis zum Umschalten'
        Write-Host '          testet die Migration auf einem eigenen Branch.'
    } elseif ($endpoint) {
        Set-BranchControl $endpoint $project.id
    } else {
        Write-Host "  [fehlt] Branch-Prüfung an $ServiceConnectionName" -ForegroundColor Yellow
        $missing.Add("Branch-Prüfung an $ServiceConnectionName")
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
    if ($SkipScaffold) {
        Write-Host '7. Azure DevOps: seed-scaffold entfällt (-SkipScaffold)'
    } else {
        Write-Host "7. Azure DevOps: seed-scaffold$(if ($CentralScaffold) { ' (zentral, Job-Token)' })"
        $scaffoldEndpoint = $null
        if ($CentralScaffold) {
            # Eigene Identität nur für den State und die Namensprüfung: Das zentrale Scaffold
            # nutzt die Deployment-Identität nicht, es teilt deren Service Connection nur weiter.
            $scaffoldIdentity = Invoke-Step "Managed Identity $ScaffoldIdentityName" {
                Get-OrNull { az identity show --name $ScaffoldIdentityName --resource-group $StateResourceGroup -o json 2>$null | ConvertFrom-Json }
            } {
                az identity create --name $ScaffoldIdentityName --resource-group $StateResourceGroup --location $Location `
                    --tags 'seed:purpose=scaffold' -o json | ConvertFrom-Json
            }
            if ($scaffoldIdentity) {
                foreach ($role in @(
                        @{ Scope = $StateContainerScope; Role = 'Storage Blob Data Contributor'; Title = "Storage Blob Data Contributor auf Container $StateContainer" }
                        @{ Scope = $Subscription; Role = 'Reader'; Title = 'Reader auf der Subscription (Namensprüfung)' })) {
                    # Nach Principal-ID filtern statt --assignee: Das sucht im Verzeichnis, und eine
                    # frisch angelegte Managed Identity ist dort oft noch nicht angekommen.
                    Invoke-Step "$($role.Title) für $ScaffoldIdentityName" {
                        az role assignment list --scope $role.Scope -o json | ConvertFrom-Json | Where-Object {
                            $_.principalId -eq $scaffoldIdentity.principalId -and $_.roleDefinitionName -eq $role.Role -and $_.scope -eq $role.Scope
                        }
                    } {
                        az role assignment create --assignee-object-id $scaffoldIdentity.principalId --assignee-principal-type ServicePrincipal `
                            --role $role.Role --scope $role.Scope -o none
                        $true
                    } | Out-Null
                }
                $scaffoldEndpoint = Invoke-Step "Service Connection $ScaffoldConnectionName (Workload Identity Federation)" {
                    Get-Endpoint $ScaffoldConnectionName
                } {
                    $config = @{
                        name                             = $ScaffoldConnectionName
                        type                             = 'AzureRM'
                        url                              = 'https://management.azure.com/'
                        description                      = "Sentinel Seed: zentrales Scaffold, Managed Identity $ScaffoldIdentityName (State und Lesen)"
                        authorization                    = @{ scheme = 'WorkloadIdentityFederation'; parameters = @{ tenantid = $tenantId; serviceprincipalid = $scaffoldIdentity.clientId } }
                        data                             = @{ environment = 'AzureCloud'; scopeLevel = 'Subscription'; subscriptionId = $SubscriptionId; subscriptionName = $subscriptionName; creationMode = 'Manual' }
                        isShared                         = $false
                        isReady                          = $true
                        serviceEndpointProjectReferences = @(@{ projectReference = @{ id = $project.id; name = $project.name }; name = $ScaffoldConnectionName })
                    }
                    az devops service-endpoint create --org $Org --project $AzureDevOpsProject --service-endpoint-configuration (Save-Json $config 'scaffold-endpoint.json') -o none 2>$null
                    Get-Endpoint $ScaffoldConnectionName
                }
            }
            if (-not $scaffoldIdentity) {
                foreach ($title in @('Rollen auf State-Container und Subscription', "Service Connection $ScaffoldConnectionName", 'Federated Credential')) {
                    Write-Host "  [fehlt] $title (nach der Managed Identity)" -ForegroundColor Yellow
                    $missing.Add($title)
                }
            }
            if ($scaffoldEndpoint -and $scaffoldIdentity) {
                $issuer = $scaffoldEndpoint.authorization.parameters.workloadIdentityFederationIssuer
                $subject = $scaffoldEndpoint.authorization.parameters.workloadIdentityFederationSubject
                Invoke-Step "Federated Credential an $ScaffoldIdentityName" {
                    az identity federated-credential list --identity-name $ScaffoldIdentityName --resource-group $StateResourceGroup -o json |
                        ConvertFrom-Json | Where-Object subject -eq $subject
                } {
                    az identity federated-credential create --name "ado-$ScaffoldConnectionName" --identity-name $ScaffoldIdentityName `
                        --resource-group $StateResourceGroup --issuer $issuer --subject $subject --audiences api://AzureADTokenExchange -o none
                    $true
                } | Out-Null
            }

            # Die Build-Service-Identität dieses Projekts teilt die Service Connections in die
            # Zielprojekte; dafür braucht sie die Rolle Administrator auf Organisationsebene.
            $buildIdentityName = "$($project.name) Build Service ($($Org.Split('/')[-1]))"
            $buildIdentity = Find-BuildIdentity $buildIdentityName
            if (-not $buildIdentity) {
                Write-Host "  [fehlt] $buildIdentityName" -ForegroundColor Yellow
                $missing.Add($buildIdentityName)
            }
            foreach ($connection in @($endpoint, $github)) {
                if (-not ($buildIdentity -and $connection)) { continue }
                $roles = "$Org/_apis/securityroles/scopes/distributedtask.collection.serviceendpointrole/roleassignments/resources/collection_$($connection.id)?api-version=7.1-preview.1"
                Invoke-Step "Administrator (Organisation) an $($connection.name) für $buildIdentityName" {
                    (Invoke-AzDevOps get $roles $null).value | Where-Object { $_.identity.id -eq $buildIdentity.Id -and $_.role.name -eq 'Administrator' }
                } {
                    Invoke-AzDevOps put $roles @(@{ roleName = 'Administrator'; userId = $buildIdentity.Id }) | Out-Null
                    $true
                } | Out-Null
            }
        }

        $repo = Invoke-Step 'Repo seed-scaffold' {
            az repos list --org $Org --project $AzureDevOpsProject -o json | ConvertFrom-Json | Where-Object name -eq 'seed-scaffold'
        } {
            az repos create --org $Org --project $AzureDevOpsProject --name seed-scaffold -o json | ConvertFrom-Json
        }
        if ($repo) {
            Invoke-Step 'azure-pipelines.yml in seed-scaffold' {
                (Invoke-AzDevOps get "$Org/$($project.id)/_apis/git/repositories/$($repo.id)/refs?filter=heads/main&api-version=7.1" $null).count -gt 0
            } {
                # Mit Freigabe-Gruppe darf die auslösende Person nicht selbst freigeben; ohne Gruppe
                # bliebe sonst oft niemand, der freigeben darf.
                $content = if ($CentralScaffold) {
                    (Get-Content -Raw (Join-Path $PSScriptRoot '..' 'scaffold' 'central-pipelines.yml')).
                        Replace('__ALLOWED_PROJECTS__', $AllowedProjects.Replace("'", "''")).
                        Replace('__MEMBER_GROUP__', $MemberGroup.Replace("'", "''")).
                        Replace('__APPROVER_GROUPS__', (ConvertTo-YamlList $ApproverGroups)).
                        Replace('__REQUESTER_CAN_APPROVE__', $(if ($ApproverGroups) { 'false' } else { 'true' })).
                        Replace('__MIN_REVIEWERS__', "$MinReviewers").
                        Replace('__SCAFFOLD_CONNECTION__', $ScaffoldConnectionName).
                        Replace('__PROJECT_POOL__', $ProjectPool.Replace("'", "''"))
                } else {
                    (Get-Content -Raw (Join-Path $PSScriptRoot '..' 'scaffold' 'azure-pipelines.yml')).
                        Replace('__APPROVERS__', (ConvertTo-YamlList $Approvers))
                }
                $content = $content.
                    Replace('__SEED_PIPELINES_VERSION__', $SeedPipelinesVersion).
                    Replace('__GITHUB_CONNECTION__', $GitHubConnectionName).
                    Replace('__SERVICE_CONNECTION__', $ServiceConnectionName).
                    Replace('__STATE_RESOURCE_GROUP__', $StateResourceGroup).
                    Replace('__STATE_STORAGE_ACCOUNT__', $StateStorageAccount).
                    Replace('__STATE_CONTAINER__', $StateContainer).
                    Replace('__POOL__', $Pool)
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
        # Das zentrale Scaffold nutzt nur seine eigene Service Connection und die für GitHub; die
        # Deployment-Identität teilt es lediglich in die Zielprojekte.
        $azureForScaffold = if ($CentralScaffold) { $scaffoldEndpoint } else { $endpoint }
        if ($pipeline -and $azureForScaffold -and $github -and -not $DryRun) {
            $resources = @("endpoint/$($azureForScaffold.id)", "endpoint/$($github.id)")
            if ($Pool) {
                $queue = (Invoke-AzDevOps get "$Org/$($project.id)/_apis/distributedtask/queues?queueNames=$([uri]::EscapeDataString($Pool))&api-version=7.1" $null).value |
                    Select-Object -First 1
                if ($queue) { $resources += "queue/$($queue.id)" }
            }
            foreach ($resource in $resources) {
                Invoke-AzDevOps patch "$Org/$($project.id)/_apis/pipelines/pipelinePermissions/$($resource)?api-version=7.1-preview.1" @{
                    pipelines = @(@{ id = $pipeline.id; authorized = $true })
                } | Out-Null
            }
            Write-Host "  [ok]    seed-scaffold berechtigt für $($azureForScaffold.name), $($github.name)$(if ($Pool) { " und den Pool $Pool" })"
        }
    }

    # --- 8. GitHub-Service-Connection für alle Pipelines ------------------------------------
    Write-Host '8. Azure DevOps: GitHub-Service-Connection für alle Pipelines'
    if ($github) {
        $githubPermissionsUrl = "$Org/$($project.id)/_apis/pipelines/pipelinePermissions/endpoint/$($github.id)?api-version=7.1-preview.1"
        Invoke-Step "GitHub-Service-Connection für alle Pipelines" {
            (Invoke-AzDevOps get $githubPermissionsUrl $null).allPipelines.authorized
        } {
            Invoke-AzDevOps patch $githubPermissionsUrl @{ allPipelines = @{ authorized = $true } } | Out-Null
            $true
        } | Out-Null
    } else {
        Write-Host '          folgt, sobald die GitHub-Service-Connection besteht (Schritt 6)'
    }

    # --- 9. Power Platform -------------------------------------------------------------------
    # Custom Connectors liegen in Dataverse (Tabelle connector). Environment Maker darf eigene
    # Connectors anlegen, ändern und teilen; mehr braucht die Pipeline nicht.
    if ($PowerPlatformEnvironmentUrl) {
        Write-Host '9. Power Platform'
        foreach ($environmentUrl in $PowerPlatformEnvironmentUrl | ForEach-Object { $_.TrimEnd('/') }) {
            $token = az account get-access-token --resource $environmentUrl --query accessToken -o tsv
            $dv = @{ Headers = @{ Authorization = "Bearer $token"; Accept = 'application/json' }; ContentType = 'application/json' }
            $api = "$environmentUrl/api/data/v9.2"
            $rootUnit = (Invoke-RestMethod @dv "$api/businessunits?`$select=businessunitid&`$filter=_parentbusinessunitid_value eq null").value[0].businessunitid
            # Die Rolle hat in der Root-Business-Unit überall dieselbe ID; der Name kann je nach
            # Sprache der Umgebung abweichen und dient nur als Rückfall.
            $role = (Invoke-RestMethod @dv "$api/roles?`$select=roleid,name&`$filter=_businessunitid_value eq $rootUnit and (roleid eq d58407f2-48d5-e711-a82c-000d3a37c848 or name eq 'Environment Maker')").value |
                Select-Object -First 1
            if (-not $role) { throw "Rolle Environment Maker in $environmentUrl nicht gefunden." }
            $findUser = { if ($app) { (Invoke-RestMethod @dv "$api/systemusers?`$select=systemuserid&`$filter=applicationid eq $($app.appId)").value | Select-Object -First 1 } }
            $user = Invoke-Step "Anwendungsbenutzer $IdentityName in $environmentUrl" $findUser {
                $body = @{ applicationid = $app.appId; 'businessunitid@odata.bind' = "/businessunits($rootUnit)" } | ConvertTo-Json
                Invoke-RestMethod @dv -Method Post -Body $body "$api/systemusers" | Out-Null
                & $findUser
            }
            Invoke-Step "Rolle $($role.name) für $IdentityName in $environmentUrl" {
                if ($user) { (Invoke-RestMethod @dv "$api/systemusers($($user.systemuserid))/systemuserroles_association?`$select=roleid").value | Where-Object roleid -eq $role.roleid }
            } {
                $body = @{ '@odata.id' = "$api/roles($($role.roleid))" } | ConvertTo-Json
                Invoke-RestMethod @dv -Method Post -Body $body "$api/systemusers($($user.systemuserid))/systemuserroles_association/`$ref" | Out-Null
                $true
            } | Out-Null
        }
    }

    # --- Ergebnis ---------------------------------------------------------------------------
    Write-Host ''
    if ($missing.Count) {
        Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" }
    } elseif ($SkipScaffold) {
        Write-Host 'Fertig. Bestehendes Repo als Seed-Projekt einrichten: onboarding/Initialize-SeedProject.ps1.' -ForegroundColor Green
        Write-Host 'Werte für azure-pipelines.yml des Projekts:'
    } elseif ($CentralScaffold) {
        Write-Host 'Fertig, ohne PAT. Noch offen (README, Zentrales Scaffold):' -ForegroundColor Green
        Write-Host '  - in diesem Projekt: Entwicklerinnen und Entwickler als Readers, an der Pipeline seed-scaffold "Queue builds"'
        Write-Host '  - je Zielprojekt einmal, durch einen Projektadmin: onboarding/Grant-SeedScaffoldAccess.ps1'
        Write-Host 'Werte für azure-pipelines.yml eines Projekts:'
    } else {
        Write-Host 'Fertig. Noch offen: PAT als geheime Variable SeedScaffoldPat an der Pipeline seed-scaffold (siehe Hilfe).' -ForegroundColor Green
        Write-Host 'Werte für azure-pipelines.yml eines Projekts:'
    }
    Write-Host "  serviceConnection: $ServiceConnectionName"
    Write-Host "  terraformState: resourceGroup $StateResourceGroup, storageAccount $StateStorageAccount, container $StateContainer"
}
finally {
    if ($previousSubscription -and $previousSubscription -ne $SubscriptionId) {
        az account set --subscription $previousSubscription
    }
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
