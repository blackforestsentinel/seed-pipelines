#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: Intune-Pipeline für die Edge-Erweiterungen einrichten, einmal je Tenant.

.DESCRIPTION
    Legt an, was die Pipeline seed-intune (templates/intune-extensions.yml) braucht. Sie besitzt
    die eine Intune-Richtlinie mit ExtensionInstallForcelist für alle Seed-Erweiterungen; die
    Liste steht in der Registerdatei extensions.yaml im Repo seed-tenant. Jeder Schritt prüft
    zuerst und legt nur an, was fehlt; das Skript lässt sich also gefahrlos wiederholen.

      1. Identität sentinel-seed-intune: App-Registrierung und Service Principal, ohne Secret
      2. Microsoft Graph: DeviceManagementConfiguration.ReadWrite.All mit Administratorzustimmung
         (appRoleAssignment). Andere Identitäten mit diesem Recht listet das Skript nur auf.
      3. Azure: leere Resource Group rg-seed-intune und Reader darauf. Die AzureRM-Service-
         Connection braucht eine Subscription, und AzureCLI@2 wählt sie nach der Anmeldung aus;
         dafür muss die Identität dort irgendein Recht haben. Reader auf einer leeren Resource
         Group zeigt die Subscription, gibt aber auf nichts Zugriff.
      4. Azure DevOps: Service Connection sc-seed-intune (Workload Identity Federation) und das
         Federated Credential an der App-Registrierung
      5. Checks an der Service Connection: „Branch control“ (-AllowedBranches, Standard main,
         dazu -TemplateRefs) und „Required template“ (templates/intune-extensions.yml aus
         blackforestsentinel/seed-pipelines auf refs/tags/<-SeedPipelinesVersion>). Nur Läufe von
         main, die genau dieses Template einbinden, bekommen die Identität. Vorhandene Checks mit
         anderen Werten werden aktualisiert.
      6. Environment seed-intune mit Freigabe durch -Approvers
      7. Repo seed-tenant; fehlt main, mit Startinhalt aus tenant/ (extensions.yaml leer,
         azure-pipelines.yml mit extends, README). Ein vorhandenes Repo bleibt unverändert; das
         Skript prüft nur, dass azure-pipelines.yml das Template auf -SeedPipelinesVersion einbindet.
      8. Pipeline seed-intune und ihre Berechtigungen: Service Connection, Environment,
         GitHub-Service-Connection. Service Connection und Environment dürfen nicht für alle
         Pipelines freigegeben sein; eine solche Freigabe nimmt das Skript zurück.
      9. Branch-Policies auf main von seed-tenant: Build-Validierung mit seed-intune (prüft die
         Registerdatei ohne Service Connection) und mindestens -MinimumReviewers Freigaben durch
         andere als die Person, die den PR erstellt oder zuletzt gepusht hat

    Ausführen mit einem az-Login, das
      - Anwendungsberechtigungen für Microsoft Graph zustimmen darf (Privileged Role
        Administrator oder Global Administrator) und App-Registrierungen anlegen darf,
      - in der Subscription Resource Groups anlegen und Rollen vergeben darf (Owner, oder
        Contributor und User Access Administrator),
      - Projektadministrator im Azure-DevOps-Projekt ist (Service Connection, Environment, Repo,
        Pipeline, Checks, Branch-Policies).
    Mit -DryRun reicht Lesezugriff.

    Vorher: Initialize-SeedTenant.ps1 für das Azure-DevOps-Projekt (GitHub-Service-Connection).

.PARAMETER SubscriptionId
    Subscription für die Service Connection und die leere Resource Group, etwa die, in der auch
    der Terraform-State liegt. Die Identität bekommt dort nur Reader auf -ResourceGroup.

.PARAMETER Approvers
    Principal Names (E-Mail) der Personen, die Änderungen an der Intune-Richtlinie freigeben.
    Pflicht: Ohne Freigabe würde jeder Merge Intune sofort ändern.

.PARAMETER AdoptPolicies
    Namen alter Intune-Richtlinien mit ExtensionInstallForcelist, die die Pipeline beim ersten
    Lauf übernimmt (umbenennt) statt eine neue anzulegen, etwa 'Sentinel Link: Edge-Erweiterung'.
    Landen nur im Startinhalt von azure-pipelines.yml; danach dort pflegen.

.PARAMETER SeedPipelinesVersion
    Tag von seed-pipelines, den azure-pipelines.yml in seed-tenant einbindet; daraus folgt die
    Prüfung „Required template“. Für ein Update erst dieses Skript mit dem neuen Tag ausführen,
    dann ref in seed-tenant per Pull Request anheben.

.PARAMETER AllowedBranches
    Branches von seed-tenant, deren Läufe die Service Connection nutzen dürfen, Standard
    -DefaultBranch. PR-Läufe brauchen sie nicht.

.PARAMETER TemplateRefs
    Refs von seed-pipelines, die die Branch-Prüfung zusätzlich erlaubt, Standard refs/tags/v*. Die
    Prüfung vergleicht jedes Repo des Laufs mit derselben Liste, auch seed-pipelines.

.PARAMETER MinimumReviewers
    Freigaben, die ein Pull Request auf main von seed-tenant braucht, Standard 1. Die Person, die
    ihn erstellt oder zuletzt gepusht hat, zählt nicht.

.PARAMETER DryRun
    Nur prüfen und anzeigen, was angelegt würde. Ändert nichts, auch nicht in Entra ID oder Azure.

.EXAMPLE
    ./Initialize-SeedIntune.ps1 -SubscriptionId 0000... -AzureDevOpsOrganization https://dev.azure.com/kunde `
        -Approvers it@kunde.de -AdoptPolicies 'Alte Edge-Richtlinie' -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $AzureDevOpsOrganization,
    [string] $AzureDevOpsProject = 'Sentinel Seed',
    [Parameter(Mandatory)] [string[]] $Approvers,
    [string[]] $AdoptPolicies = @(),
    [string] $PolicyName = 'Sentinel Seed: Edge-Erweiterungen',
    [string] $IdentityName = 'sentinel-seed-intune',
    [string] $ServiceConnectionName = 'sc-seed-intune',
    [string] $GitHubConnectionName = 'github-blackforestsentinel',
    [string] $ResourceGroup = 'rg-seed-intune',
    [string] $Location = 'westeurope',
    [string] $EnvironmentName = 'seed-intune',
    [string] $Repository = 'seed-tenant',
    [string] $PipelineName = 'seed-intune',
    [string] $DefaultBranch = 'main',
    [string] $SeedPipelinesVersion = 'v0.12.0',
    [string[]] $AllowedBranches = @(),
    [string[]] $TemplateRefs = @('refs/tags/v*'),
    [ValidateRange(1, 10)] [int] $MinimumReviewers = 1,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

# Listen auch als "a,b" in einem Argument annehmen, etwa beim Aufruf über pwsh -File. Namen von
# Richtlinien können Kommas enthalten und werden deshalb nicht geteilt.
function Split-List([string[]] $Values) {
    @($Values | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
# Name in deutschen Anführungszeichen. PowerShell liest „ und “ als doppelte Anführungszeichen;
# in "…" würden sie die Zeichenkette beenden, deshalb nur in '…' und über diese Funktion.
function Format-Name([string] $Name) { '„' + $Name + '“' }

$Approvers = Split-List $Approvers
$AdoptPolicies = @($AdoptPolicies | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$AllowedBranches = Split-List $AllowedBranches
if (-not $AllowedBranches) { $AllowedBranches = @($DefaultBranch) }
$AllowedBranchRefs = @(@($AllowedBranches | ForEach-Object { if ($_ -like 'refs/*') { $_ } else { "refs/heads/$_" } }) +
    @(Split-List $TemplateRefs)) -join ','
if (-not $Approvers) { throw 'Approvers: mindestens eine Person; ohne Freigabe würde jeder Merge Intune sofort ändern.' }
if ($SeedPipelinesVersion -notmatch '^v\d+\.\d+\.\d+$') { throw 'SeedPipelinesVersion: Tag wie v0.12.0 erwartet.' }
if ($PolicyName -in $AdoptPolicies) { throw ('AdoptPolicies: {0} ist der Name der Richtlinie selbst.' -f (Format-Name $PolicyName)) }
$AdoptText = ($AdoptPolicies | ForEach-Object { Format-Name $_ }) -join ', '

$Org = $AzureDevOpsOrganization.TrimEnd('/')
$orgName = if ($Org -match '^https://dev\.azure\.com/([^/]+)$') { $Matches[1] }
    elseif ($Org -match '^https://([^./]+)\.visualstudio\.com$') { $Matches[1] }
    else { throw 'AzureDevOpsOrganization: https://dev.azure.com/<organisation> erwartet.' }
$Identities = "https://vssps.dev.azure.com/$orgName/_apis/identities"
$AzureDevOpsResource = '499b84ac-1321-427f-aa17-267ca6975798'
$GraphAppId = '00000003-0000-0000-c000-000000000000'
$IntunePermission = 'DeviceManagementConfiguration.ReadWrite.All'
$ReaderRole = 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
$ApprovalCheckType = '8c6f20a7-a545-4486-9777-f762fafe0d4d'
$BranchControlCheckType = 'fe1de3ee-a436-41b4-bb20-f6eb4cb879a7'
$BranchControlDefinition = '86b05a0c-73e6-4f7d-b3cf-e38f3b39a75b'
$RequiredTemplateCheckType = '4020e66e-b0f3-47e1-bc88-48f3cc59b5f3'
$BuildPolicyType = '0609b952-1397-4640-95ec-e00a01b2c241'
$ReviewerPolicyType = 'fa4e907d-c16b-4a4c-9dfa-4906e5d171dd'
$SeedPipelinesRepository = 'blackforestsentinel/seed-pipelines'
$TemplatePath = 'templates/intune-extensions.yml'
$YamlPath = 'azure-pipelines.yml'
$BranchRef = "refs/heads/$DefaultBranch"
$ResourceGroupScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup"
$TenantFiles = @('extensions.yaml', 'azure-pipelines.yml', 'README.md')

$work = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) "seed-intune-$(Get-Random)")
$missing = [Collections.Generic.List[string]]::new()
$token = $null
$graphToken = $null

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

# Für Schritte, die an einer fehlenden Voraussetzung hängen; auch außerhalb des DryRuns.
function Write-Missing([string] $Title, [string] $Reason) {
    Write-Host "  [fehlt] $Title" -ForegroundColor Yellow
    if ($Reason) { Write-Host "          $Reason" }
    $missing.Add($Title)
}

# Vorhanden, aber mit anderen Werten: im DryRun melden, sonst mit $Update angleichen.
function Update-Step([string] $Title, [string] $Was, [scriptblock] $Update) {
    if ($DryRun) {
        Write-Host "  [alt]   $Title (bisher $Was)" -ForegroundColor Yellow
        $missing.Add($Title)
        return
    }
    & $Update | Out-Null
    Write-Host "  [neu]   $Title (bisher $Was)" -ForegroundColor Green
}

function Invoke-AzDevOps([string] $Method, [string] $Url, [object] $Body) {
    # Direkt per HTTP: URLs mit & überstehen den Umweg über az.cmd nicht. Bodies gehen als
    # UTF-8-Datei raus, damit weder Quoting noch Kodierung etwas verändern.
    if (-not $script:token) {
        $script:token = az account get-access-token --resource $AzureDevOpsResource --query accessToken -o tsv
    }
    $request = @{ Method = $Method; Uri = $Url; Headers = @{ Authorization = "Bearer $script:token" }; ContentType = 'application/json' }
    if ($null -ne $Body) {
        $request.InFile = Save-Json $Body "body-$(Get-Random).json"
    }
    Invoke-RestMethod @request
}

function Invoke-Graph([string] $Method, [string] $Url, [object] $Body) {
    if (-not $script:graphToken) {
        $script:graphToken = az account get-access-token --resource-type ms-graph --query accessToken -o tsv
    }
    $request = @{ Method = $Method; Uri = $Url; Headers = @{ Authorization = "Bearer $script:graphToken" }; ContentType = 'application/json' }
    if ($null -ne $Body) {
        $request.InFile = Save-Json $Body "graph-$(Get-Random).json"
    }
    Invoke-RestMethod @request
}

# Für Prüfungen: Fehler (etwa 404 für eine fehlende Ressource) bedeuten "nicht vorhanden".
function Get-OrNull([scriptblock] $Command) {
    try { & $Command } catch { $null }
}

# Startinhalt von seed-tenant aus tenant/ in diesem Repo, mit den Werten dieses Laufs.
function Get-TenantContent([string] $File) {
    $quote = { param($Text) "'" + $Text.Replace("'", "''") + "'" }
    $adopt = if ($AdoptPolicies) { '[' + (($AdoptPolicies | ForEach-Object { & $quote $_ }) -join ', ') + ']' } else { '[]' }
    $content = (Get-Content -Raw -Encoding utf8 -Path (Join-Path $PSScriptRoot '..' 'tenant' $File)).
        Replace('__SEED_PIPELINES_VERSION__', $SeedPipelinesVersion).
        Replace('__GITHUB_CONNECTION__', $GitHubConnectionName).
        Replace('__SERVICE_CONNECTION__', $ServiceConnectionName).
        Replace('__ENVIRONMENT__', $EnvironmentName).
        Replace('__PIPELINE__', $PipelineName).
        Replace('__POLICY_NAME__', (& $quote $PolicyName)).
        Replace('__POLICY_TEXT__', $PolicyName).
        Replace('__ADOPT_POLICIES__', $adopt)
    $left = [regex]::Matches($content, '__[A-Z_]+__') | ForEach-Object Value | Sort-Object -Unique
    if ($left) { throw "tenant/$File enthält unbekannte Platzhalter: $($left -join ', ')" }
    $content
}

# azure-pipelines.yml in seed-tenant muss das Template auf dem Tag der Prüfung „Required template“
# einbinden, sonst lehnt die Prüfung jeden Lauf auf main ab. Liefert den Grund oder nichts.
function Test-TenantYaml([string] $Content) {
    if (-not $Content) { return "$YamlPath fehlt auf $DefaultBranch" }
    if ($Content -notmatch [regex]::Escape("$TemplatePath@seed")) { return "bindet $TemplatePath@seed nicht ein" }
    if ($Content -notmatch "(?m)^\s*ref:\s*refs/tags/$([regex]::Escape($SeedPipelinesVersion))\s*$") {
        return "bindet seed-pipelines nicht auf refs/tags/$SeedPipelinesVersion ein; per Pull Request anheben"
    }
}

# Erster Check dieses Typs an der Service Connection, der $Match erfüllt.
function Get-Check([object] $Endpoint, [string] $TypeId, [scriptblock] $Match = { $true }) {
    (Invoke-AzDevOps get "$api/pipelines/checks/configurations?resourceType=endpoint&resourceId=$($Endpoint.id)&`$expand=settings&api-version=7.1-preview.1").value |
        Where-Object { $_.type.id -eq $TypeId } | Where-Object $Match | Select-Object -First 1
}

# Legt einen Check an oder gleicht ihn an; $Current liefert die Werte des vorhandenen Checks zum
# Vergleich mit $Wanted.
function Set-Check([string] $Title, [object] $Existing, [hashtable] $Body, [string] $Wanted, [scriptblock] $Current) {
    $checks = "$api/pipelines/checks/configurations"
    if ($Existing -and (& $Current $Existing) -ne $Wanted) {
        Update-Step $Title (& $Current $Existing) {
            $Body.id = $Existing.id
            Invoke-AzDevOps patch "$checks/$($Existing.id)?api-version=7.1-preview.1" $Body
        }
        return
    }
    Invoke-Step $Title { $Existing } {
        Invoke-AzDevOps post "$checks`?api-version=7.1-preview.1" $Body
    } | Out-Null
}

function Get-Policy([string] $TypeId, [scriptblock] $Match) {
    (Invoke-AzDevOps get "$api/policy/configurations?policyType=$TypeId&api-version=7.1").value | Where-Object {
        $_.type.id -eq $TypeId -and ($_.settings.scope | Where-Object { $_.repositoryId -eq $repo.id -and $_.refName -eq $BranchRef }) -and (& $Match $_)
    } | Select-Object -First 1
}

try {
    foreach ($file in $TenantFiles) { Get-TenantContent $file | Out-Null }
    $account = az account show --subscription $SubscriptionId -o json | ConvertFrom-Json
    $tenantId = $account.tenantId
    $project = Invoke-AzDevOps get "$Org/_apis/projects/$([uri]::EscapeDataString($AzureDevOpsProject))?api-version=7.1"
    $api = "$Org/$($project.id)/_apis"

    # Wie data.azuredevops_users in ado-project: genau eine aktive Person je Adresse.
    $approverIds = @(foreach ($mail in $Approvers) {
            $found = @((Invoke-AzDevOps get "$Identities`?searchFilter=General&filterValue=$([uri]::EscapeDataString($mail))&queryMembership=None&api-version=7.1").value |
                    Where-Object { $_.isActive -and ($_.properties.Account.'$value' -eq $mail -or $_.properties.Mail.'$value' -eq $mail) })
            if ($found.Count -ne 1) { throw "Freigabe: Zu $mail gibt es $($found.Count) Personen in Azure DevOps, erwartet ist genau eine." }
            $found[0].id
        })

    Write-Host "Sentinel Seed Intune-Einrichtung$(if ($DryRun) { ' (DryRun, keine Änderungen)' })"
    Write-Host "  Tenant $tenantId, Subscription $($account.name), Resource Group $ResourceGroup"
    Write-Host "  Azure DevOps $Org/$($project.name), Repo $Repository, Pipeline $PipelineName, Environment $EnvironmentName"
    Write-Host ('  Richtlinie {0}{1}, seed-pipelines {2}, Freigabe {3}' -f (Format-Name $PolicyName),
        $(if ($AdoptPolicies) { ", übernimmt $AdoptText" }), $SeedPipelinesVersion, ($Approvers -join ', '))
    Write-Host ''

    # --- 1. Identität -----------------------------------------------------------------------
    Write-Host '1. Identität'
    $app = Invoke-Step "App-Registrierung $IdentityName" {
        # --display-name sucht nach dem Anfang des Namens; deshalb nur exakte Treffer, und nur einer.
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
    } else {
        Write-Missing "Service Principal $IdentityName" 'App-Registrierung fehlt.'
    }

    # --- 2. Microsoft Graph -----------------------------------------------------------------
    Write-Host '2. Microsoft Graph'
    $graph = az ad sp show --id $GraphAppId -o json | ConvertFrom-Json
    $role = $graph.appRoles | Where-Object value -eq $IntunePermission
    if (-not $role) { throw "Die Graph-Berechtigung $IntunePermission gibt es im Tenant nicht." }
    if (-not $sp) {
        Write-Missing "$IntunePermission mit Administratorzustimmung" 'Service Principal fehlt.'
    } else {
        Invoke-Step "$IntunePermission mit Administratorzustimmung" {
            (Invoke-Graph get "https://graph.microsoft.com/v1.0/servicePrincipals/$($sp.id)/appRoleAssignments").value |
                Where-Object { $_.appRoleId -eq $role.id -and $_.resourceId -eq $graph.id }
        } {
            Invoke-Graph post "https://graph.microsoft.com/v1.0/servicePrincipals/$($sp.id)/appRoleAssignments" @{
                principalId = $sp.id; resourceId = $graph.id; appRoleId = $role.id
            } | Out-Null
            $true
        } | Out-Null
    }
    # Nur diese Identität soll Intune-Richtlinien schreiben. Andere fasst das Skript nicht an, es
    # nennt sie aber: etwa eine Rollout-Identität eines Projekts, die ihre eigene Richtlinie
    # gepflegt hat und das Recht nach der Übernahme nicht mehr braucht.
    $holders = Get-OrNull {
        $list = @()
        $url = "https://graph.microsoft.com/v1.0/servicePrincipals/$($graph.id)/appRoleAssignedTo?`$top=999"
        while ($url) {
            $page = Invoke-Graph get $url
            $list += @($page.value | Where-Object { $_.appRoleId -eq $role.id -and (-not $sp -or $_.principalId -ne $sp.id) })
            $url = $page.'@odata.nextLink'
        }
        , $list
    }
    if ($null -eq $holders) {
        Write-Host "          Andere Identitäten mit $IntunePermission ließen sich nicht lesen."
    } elseif ($holders.Count) {
        Write-Host "  [info]  Weitere Identitäten mit ${IntunePermission}: $(($holders | ForEach-Object principalDisplayName) -join ', ')" -ForegroundColor Yellow
        Write-Host '          Nach der Übernahme ihrer Richtlinien das Recht entziehen (Entra ID, Unternehmensanwendungen,'
        Write-Host '          Berechtigungen), sofern sie es nicht für anderes brauchen.'
    }

    # --- 3. Azure -----------------------------------------------------------------------------
    # Die AzureRM-Service-Connection verlangt eine Subscription, AzureCLI@2 wählt sie nach der
    # Anmeldung aus. Die Subscription sieht die Identität nur mit einem Recht darin; Reader auf
    # einer leeren Resource Group ist das kleinste, das es gibt.
    Write-Host '3. Azure: Resource Group für die Service Connection'
    $group = Invoke-Step "Resource Group $ResourceGroup" {
        (az group exists --name $ResourceGroup --subscription $SubscriptionId) -eq 'true'
    } {
        az group create --name $ResourceGroup --location $Location --subscription $SubscriptionId `
            --tags 'seed:purpose=intune-service-connection' -o none
        $true
    }
    $contents = if ($group) { @(az resource list --resource-group $ResourceGroup --subscription $SubscriptionId -o json | ConvertFrom-Json) }
    if ($contents) {
        Write-Host "  [info]  $ResourceGroup ist nicht leer ($($contents.Count) Ressourcen); Reader darauf kann sie lesen." -ForegroundColor Yellow
        Write-Host '          Gedacht ist eine eigene, leere Resource Group (-ResourceGroup).'
    }
    if (-not $sp -or -not $group) {
        Write-Missing "Reader auf $ResourceGroup" $(if (-not $sp) { 'Service Principal fehlt.' } else { 'Resource Group fehlt.' })
    } else {
        Invoke-Step "Reader auf $ResourceGroup" {
            az role assignment list --assignee $sp.id --scope $ResourceGroupScope --role $ReaderRole --subscription $SubscriptionId --query '[0].id' -o tsv
        } {
            # Direkt nach dem Anlegen kennt Azure den Service Principal unter Umständen noch nicht.
            for ($attempt = 1; ; $attempt++) {
                try {
                    az role assignment create --assignee-object-id $sp.id --assignee-principal-type ServicePrincipal `
                        --role $ReaderRole --scope $ResourceGroupScope --subscription $SubscriptionId -o none
                    break
                } catch {
                    if ($attempt -eq 12) { throw }
                    Write-Host '          Service Principal noch nicht verteilt, neuer Versuch in 10 Sekunden'
                    Start-Sleep -Seconds 10
                }
            }
            $true
        } | Out-Null
    }
    $others = if ($sp) {
        @(az role assignment list --assignee $sp.id --all -o json | ConvertFrom-Json | Where-Object scope -ne $ResourceGroupScope)
    }
    if ($others) {
        Write-Host "  [info]  $IdentityName hat weitere Azure-Rollen, nötig ist nur Reader auf ${ResourceGroup}:" -ForegroundColor Yellow
        $others | ForEach-Object { Write-Host "          $($_.roleDefinitionName) auf $($_.scope)" }
    }

    # --- 4. Service Connection --------------------------------------------------------------
    Write-Host '4. Azure DevOps: Service Connection'
    function Get-Endpoint([string] $Name) {
        (Invoke-AzDevOps get "$api/serviceendpoint/endpoints?endpointNames=$([uri]::EscapeDataString($Name))&api-version=7.1").value |
            Where-Object name -eq $Name | Select-Object -First 1
    }
    $endpoint = if (-not $app) { Write-Missing "Service Connection $ServiceConnectionName" 'App-Registrierung fehlt.' } else {
        Invoke-Step "Service Connection $ServiceConnectionName (Workload Identity Federation)" {
            Get-Endpoint $ServiceConnectionName
        } {
            Invoke-AzDevOps post "$Org/_apis/serviceendpoint/endpoints?api-version=7.1" @{
                name          = $ServiceConnectionName
                type          = 'AzureRM'
                url           = 'https://management.azure.com/'
                description   = "Sentinel Seed: Identität $IdentityName, Intune-Richtlinie der Edge-Erweiterungen (Pipeline $PipelineName). In Azure nur Reader auf $ResourceGroup."
                authorization = @{ scheme = 'WorkloadIdentityFederation'; parameters = @{ tenantid = $tenantId; serviceprincipalid = $app.appId } }
                data          = @{ environment = 'AzureCloud'; scopeLevel = 'Subscription'; subscriptionId = $SubscriptionId; subscriptionName = $account.name; creationMode = 'Manual' }
                isShared      = $false
                isReady       = $true
                serviceEndpointProjectReferences = @(@{ projectReference = @{ id = $project.id; name = $project.name }; name = $ServiceConnectionName })
            } | Out-Null
            Get-Endpoint $ServiceConnectionName
        }
    }
    $endpointIdentity = $endpoint.authorization.parameters.serviceprincipalid
    if ($endpoint -and $endpointIdentity -and $endpointIdentity -ne $app.appId) {
        throw "Service Connection $ServiceConnectionName gibt es schon, aber für eine andere Identität ($($endpoint.authorization.parameters.serviceprincipalid)). Anderen -ServiceConnectionName wählen."
    }
    if ($endpoint) {
        $subject = $endpoint.authorization.parameters.workloadIdentityFederationSubject
        Invoke-Step 'Federated Credential an der App-Registrierung' {
            az ad app federated-credential list --id $app.appId -o json | ConvertFrom-Json | Where-Object subject -eq $subject
        } {
            $file = Save-Json @{
                # Eindeutig je App-Registrierung; erlaubt sind nur Buchstaben, Ziffern, - und _.
                name        = "ado-$($project.name)-$ServiceConnectionName" -replace '[^A-Za-z0-9_-]', '-'
                issuer      = $endpoint.authorization.parameters.workloadIdentityFederationIssuer
                subject     = $subject
                audiences   = @('api://AzureADTokenExchange')
                description = "Azure DevOps $Org / $($project.name) / $ServiceConnectionName"
            } 'credential.json'
            az ad app federated-credential create --id $app.appId --parameters $file -o none
            $true
        } | Out-Null
    }

    # --- 5. Checks an der Service Connection -------------------------------------------------
    # Freigaben hängen am Environment. Eine geänderte Pipeline-Datei auf einem Branch oder ein
    # anderes Template, das die Service Connection direkt nutzt, hielten sie nicht auf; diese
    # beiden Checks schon: nur main, und nur mit templates/intune-extensions.yml auf dem Tag.
    Write-Host '5. Checks an der Service Connection'
    if (-not $endpoint) {
        Write-Missing "Branch-Prüfung an ${ServiceConnectionName}: $AllowedBranchRefs" 'Service Connection fehlt.'
        Write-Missing "Required template an ${ServiceConnectionName}: $TemplatePath@refs/tags/$SeedPipelinesVersion" 'Service Connection fehlt.'
    } else {
        Set-Check "Branch-Prüfung an ${ServiceConnectionName}: $AllowedBranchRefs" (Get-Check $endpoint $BranchControlCheckType { $_.settings.definitionRef.id -eq $BranchControlDefinition }) @{
            type     = @{ id = $BranchControlCheckType; name = 'Task Check' }
            resource = @{ type = 'endpoint'; id = "$($endpoint.id)"; name = $endpoint.name }
            settings = @{
                definitionRef = @{ id = $BranchControlDefinition; name = 'evaluatebranchProtection'; version = '0.0.1' }
                displayName   = 'Sentinel Seed: nur freigegebene Branches'
                inputs        = @{ allowedBranches = $AllowedBranchRefs; ensureProtectionOfBranch = 'false'; allowUnknownStatusBranch = 'false' }
            }
            timeout  = 1440
        } $AllowedBranchRefs { param($Check) "$($Check.settings.inputs.allowedBranches)" }

        $required = @{ repositoryType = 'github'; repositoryName = $SeedPipelinesRepository; repositoryRef = "refs/tags/$SeedPipelinesVersion"; templatePath = $TemplatePath }
        $describe = { param($Items) (@($Items) | ForEach-Object { "$($_.repositoryType):$($_.repositoryName)/$($_.templatePath)@$($_.repositoryRef)" }) -join ', ' }
        Set-Check "Required template an ${ServiceConnectionName}: $TemplatePath@refs/tags/$SeedPipelinesVersion" (Get-Check $endpoint $RequiredTemplateCheckType) @{
            type     = @{ id = $RequiredTemplateCheckType; name = 'ExtendsCheck' }
            resource = @{ type = 'endpoint'; id = "$($endpoint.id)"; name = $endpoint.name }
            settings = @{ extendsChecks = @($required) }
        } (& $describe $required) { param($Check) & $describe $Check.settings.extendsChecks }
    }

    # --- 6. Environment -----------------------------------------------------------------------
    Write-Host '6. Azure DevOps: Environment und Freigabe'
    $environment = Invoke-Step "Environment $EnvironmentName" {
        (Invoke-AzDevOps get "$api/distributedtask/environments?name=$([uri]::EscapeDataString($EnvironmentName))&api-version=7.1").value |
            Where-Object name -eq $EnvironmentName | Select-Object -First 1
    } {
        Invoke-AzDevOps post "$api/distributedtask/environments?api-version=7.1" @{
            name = $EnvironmentName; description = ('Intune-Richtlinie {0} (Pipeline {1})' -f (Format-Name $PolicyName), $PipelineName)
        }
    }
    Invoke-Step "Freigabe an $EnvironmentName" {
        if ($environment) {
            (Invoke-AzDevOps get "$api/pipelines/checks/configurations?resourceType=environment&resourceId=$($environment.id)&api-version=7.1-preview.1").value |
                Where-Object { $_.type.id -eq $ApprovalCheckType }
        }
    } {
        Invoke-AzDevOps post "$api/pipelines/checks/configurations?api-version=7.1-preview.1" @{
            type     = @{ id = $ApprovalCheckType; name = 'Approval' }
            resource = @{ type = 'environment'; id = "$($environment.id)"; name = $environment.name }
            settings = @{
                approvers                 = @($approverIds | ForEach-Object { @{ id = $_ } })
                executionOrder            = 'anyOrder'
                minRequiredApprovers      = 1
                requesterCannotBeApprover = $false
                instructions              = 'Änderungen an der Intune-Richtlinie im Log der Stage Plan prüfen (entfernte Erweiterungen deinstalliert Edge auf allen Geräten), dann freigeben.'
                blockedApprovers          = @()
            }
            timeout  = 43200
        }
    } | Out-Null

    # --- 7. Repo seed-tenant ------------------------------------------------------------------
    Write-Host "7. Azure DevOps: Repo $Repository"
    $repo = Invoke-Step "Repo $Repository" {
        Get-OrNull { Invoke-AzDevOps get "$api/git/repositories/$([uri]::EscapeDataString($Repository))?api-version=7.1" }
    } {
        Invoke-AzDevOps post "$api/git/repositories?api-version=7.1" @{ name = $Repository; project = @{ id = $project.id } }
    }
    $hasBranch = [bool] ($repo -and ((Invoke-AzDevOps get "$api/git/repositories/$($repo.id)/refs?filter=$([uri]::EscapeDataString("heads/$DefaultBranch"))&api-version=7.1").value |
                Where-Object name -eq $BranchRef))
    if (-not $repo) {
        Write-Missing "Startinhalt in $Repository ($($TenantFiles -join ', '))" 'Repo fehlt.'
    } elseif (-not $hasBranch) {
        Invoke-Step "Startinhalt in $Repository ($($TenantFiles -join ', '))" { $null } {
            Invoke-AzDevOps post "$api/git/repositories/$($repo.id)/pushes?api-version=7.1" @{
                refUpdates = @(@{ name = $BranchRef; oldObjectId = '0000000000000000000000000000000000000000' })
                commits    = @(@{
                        comment = 'Sentinel Seed: Startinhalt seed-tenant (onboarding/Initialize-SeedIntune.ps1)'
                        changes = @($TenantFiles | ForEach-Object {
                                @{ changeType = 'add'; item = @{ path = "/$_" }; newContent = @{ content = (Get-TenantContent $_); contentType = 'rawtext' } }
                            })
                    })
            } | Out-Null
            $true
        } | Out-Null
    } else {
        Write-Host "  [ok]    Branch $DefaultBranch in $Repository (Inhalt bleibt unverändert)"
        $yaml = Get-OrNull {
            (Invoke-AzDevOps get "$api/git/repositories/$($repo.id)/items?path=$([uri]::EscapeDataString($YamlPath))&versionDescriptor.version=$([uri]::EscapeDataString($DefaultBranch))&versionDescriptor.versionType=branch&includeContent=true&`$format=json&api-version=7.1").content
        }
        $yamlProblem = Test-TenantYaml $yaml
        if ($yamlProblem) {
            Write-Missing "$YamlPath in $Repository passt zur Prüfung Required template" $yamlProblem
        } else {
            Write-Host "  [ok]    $YamlPath bindet $TemplatePath auf $SeedPipelinesVersion ein"
        }
    }

    # --- 8. Pipeline --------------------------------------------------------------------------
    Write-Host "8. Azure DevOps: Pipeline $PipelineName"
    $definition = Invoke-Step "Pipeline $PipelineName ($Repository/$YamlPath)" {
        $reference = (Invoke-AzDevOps get "$api/build/definitions?name=$([uri]::EscapeDataString($PipelineName))&api-version=7.1").value |
            Where-Object name -eq $PipelineName | Select-Object -First 1
        if ($reference) {
            $existing = Invoke-AzDevOps get "$api/build/definitions/$($reference.id)?api-version=7.1"
            if (-not $repo -or $existing.repository.id -ne $repo.id -or "$($existing.process.yamlFilename)".TrimStart('/') -ne $YamlPath) {
                throw "Pipeline $PipelineName gibt es schon, aber für $($existing.repository.name) $($existing.process.yamlFilename). Anderen -PipelineName wählen."
            }
            $existing
        }
    } {
        $queue = (Invoke-AzDevOps get "$api/distributedtask/queues?queueNames=$([uri]::EscapeDataString('Azure Pipelines'))&api-version=7.1").value |
            Select-Object -First 1
        if (-not $queue) { throw 'Die Agent-Queue "Azure Pipelines" fehlt im Projekt.' }
        Invoke-AzDevOps post "$api/build/definitions?api-version=7.1" @{
            name        = $PipelineName
            path        = '\'
            type        = 'build'
            quality     = 'definition'
            queueStatus = 'enabled'
            queue       = @{ id = $queue.id; name = $queue.name }
            repository  = @{
                id            = $repo.id
                name          = $repo.name
                type          = 'TfsGit'
                url           = $repo.remoteUrl
                defaultBranch = $BranchRef
                properties    = @{ reportBuildStatus = 'true' }
            }
            process     = @{ type = 2; yamlFilename = $YamlPath }
            # Trigger aus der YAML-Datei: Änderungen an der Registerdatei auf main
            triggers    = @(@{
                    triggerType                  = 'continuousIntegration'
                    settingsSourceType           = 2
                    branchFilters                = @()
                    pathFilters                  = @()
                    batchChanges                 = $false
                    maxConcurrentBuildsPerBranch = 1
                })
        }
    }
    $github = (Invoke-AzDevOps get "$api/serviceendpoint/endpoints?endpointNames=$([uri]::EscapeDataString($GitHubConnectionName))&api-version=7.1").value |
        Where-Object name -eq $GitHubConnectionName | Select-Object -First 1
    $resources = @(
        @{ Title = "Service Connection $ServiceConnectionName"; Type = 'endpoint'; Id = $endpoint.id; Exclusive = $true; Reason = 'Service Connection fehlt.' }
        @{ Title = "Environment $EnvironmentName"; Type = 'environment'; Id = $environment.id; Exclusive = $true; Reason = 'Environment fehlt.' }
        @{ Title = "GitHub-Service-Connection $GitHubConnectionName"; Type = 'endpoint'; Id = $github.id; Reason = 'GitHub-Service-Connection fehlt im Projekt: zuerst Initialize-SeedTenant.ps1 ausführen.' }
    )
    foreach ($resource in $resources) {
        $title = "Pipeline berechtigt für $($resource.Title)"
        if (-not $resource.Id) {
            Write-Missing $title $resource.Reason
            continue
        }
        $permissionsUrl = "$api/pipelines/pipelinePermissions/$($resource.Type)/$($resource.Id)?api-version=7.1-preview.1"
        $permissions = Invoke-AzDevOps get $permissionsUrl
        if (-not $definition) {
            Write-Missing $title 'Pipeline fehlt.'
        } else {
            Invoke-Step $title {
                $permissions.pipelines | Where-Object { $_.id -eq $definition.id -and $_.authorized }
            } {
                Invoke-AzDevOps patch $permissionsUrl @{ pipelines = @(@{ id = $definition.id; authorized = $true }) } | Out-Null
                $true
            } | Out-Null
        }
        # Die Identität darf Intune ändern: Nur diese Pipeline soll Service Connection und
        # Environment bekommen, nicht jede im Projekt.
        if ($resource.Exclusive -and $permissions.allPipelines.authorized) {
            Update-Step "$($resource.Title) nur für $PipelineName" 'für alle Pipelines freigegeben' {
                Invoke-AzDevOps patch $permissionsUrl @{ allPipelines = @{ authorized = $false } }
            }
        }
    }

    # --- 9. Branch-Policies -------------------------------------------------------------------
    # Die Registerdatei entscheidet, was auf jedem verwalteten Gerät installiert wird: Änderungen
    # nur per Pull Request, geprüft von der Pipeline (Format, updates.xml, ohne Service
    # Connection) und von einer zweiten Person. Die Freigabe im Environment prüft danach den
    # konkreten Plan gegen Intune. Als Pflicht-Policies sperren beide direkte Pushes auf main.
    Write-Host "9. Branch-Policies auf $DefaultBranch von $Repository"
    $buildTitle = "Build-Validierung mit $PipelineName"
    $reviewTitle = "Mindestens $MinimumReviewers Freigabe(n), nicht durch die Person des PRs"
    if (-not $repo -or -not $hasBranch) {
        Write-Missing $buildTitle "Branch $DefaultBranch in $Repository fehlt noch."
        Write-Missing $reviewTitle "Branch $DefaultBranch in $Repository fehlt noch."
    } else {
        if (-not $definition) {
            Write-Missing $buildTitle 'Pipeline fehlt.'
        } else {
            Invoke-Step $buildTitle {
                Get-Policy $BuildPolicyType { param($Policy) $Policy.settings.buildDefinitionId -eq $definition.id }
            } {
                Invoke-AzDevOps post "$api/policy/configurations?api-version=7.1" @{
                    isEnabled  = $true
                    isBlocking = $true
                    type       = @{ id = $BuildPolicyType }
                    settings   = @{
                        buildDefinitionId       = $definition.id
                        displayName             = 'Registerdatei prüfen'
                        manualQueueOnly         = $false
                        # Ein Ergebnis gilt 12 Stunden, neu geprüft wird nur bei Änderungen am PR.
                        queueOnSourceUpdateOnly = $true
                        validDuration           = 720
                        scope                   = @(@{ repositoryId = $repo.id; refName = $BranchRef; matchKind = 'Exact' })
                    }
                } | Out-Null
                $true
            } | Out-Null
        }
        $reviewers = Get-Policy $ReviewerPolicyType { param($Policy) $true }
        if ($reviewers -and ($reviewers.settings.minimumApproverCount -lt $MinimumReviewers -or $reviewers.settings.creatorVoteCounts -or -not $reviewers.isBlocking)) {
            Update-Step $reviewTitle "$($reviewers.settings.minimumApproverCount), eigene Stimme zählt: $($reviewers.settings.creatorVoteCounts), Pflicht: $($reviewers.isBlocking)" {
                $reviewers.isBlocking = $true
                $reviewers.isEnabled = $true
                $reviewers.settings.minimumApproverCount = [Math]::Max($MinimumReviewers, [int] $reviewers.settings.minimumApproverCount)
                $reviewers.settings.creatorVoteCounts = $false
                Invoke-AzDevOps put "$api/policy/configurations/$($reviewers.id)?api-version=7.1" $reviewers
            }
        } else {
            Invoke-Step $reviewTitle { $reviewers } {
                Invoke-AzDevOps post "$api/policy/configurations?api-version=7.1" @{
                    isEnabled  = $true
                    isBlocking = $true
                    type       = @{ id = $ReviewerPolicyType }
                    settings   = @{
                        minimumApproverCount = $MinimumReviewers
                        creatorVoteCounts    = $false
                        blockLastPusherVote  = $true
                        allowDownvotes       = $false
                        # Eine Freigabe gilt nur für den Stand, den die Person gesehen hat.
                        resetOnSourcePush    = $true
                        scope                = @(@{ repositoryId = $repo.id; refName = $BranchRef; matchKind = 'Exact' })
                    }
                } | Out-Null
                $true
            } | Out-Null
        }
    }

    # --- Ergebnis -----------------------------------------------------------------------------
    Write-Host ''
    if ($missing.Count) {
        Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" }
    } else {
        Write-Host 'Fertig.' -ForegroundColor Green
        Write-Host "Erweiterungen kommen per Pull Request in $Repository/extensions.yaml; nach dem Merge plant"
        Write-Host "$PipelineName die Änderung und wartet auf die Freigabe im Environment $EnvironmentName."
        if ($AdoptPolicies) {
            Write-Host "Vor dem ersten Lauf: Die alten Pipelines, die $AdoptText pflegen, anhalten" -ForegroundColor Yellow
            Write-Host 'und ihre Einträge in extensions.yaml übernehmen; sonst legen sie die Richtlinie nach der Übernahme neu an.' -ForegroundColor Yellow
        }
    }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
