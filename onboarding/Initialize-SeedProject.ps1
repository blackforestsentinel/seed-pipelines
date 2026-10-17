#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: bestehendes Repo als Seed-Projekt einrichten (Migration).

.DESCRIPTION
    Legt in Azure DevOps an, was die Projekt-Pipeline aus seed-pipelines braucht, wie das
    Terraform-Modul seed-terraform//ado-project für neue Projekte, aber ohne Repo-Import und
    ohne Dateien zu schreiben: project.yaml und azure-pipelines.yml stellt das Projekt selbst
    um, auf einem eigenen Branch. Jeder Schritt prüft zuerst und legt nur an, was fehlt; das
    Skript lässt sich also gefahrlos wiederholen.

      1. Environments <projekt>-<umgebung> (Infrastruktur, mit Freigabe) und
         <projekt>-<umgebung>-app (App-Deploy, mit Freigabe nur in -AppApprovalEnvironments)
      2. Pipeline-Definition für die YAML-Datei, ohne sie zu starten
      3. Berechtigungen der Pipeline für die Environments, die Azure-Service-Connection und
         die GitHub-Service-Connection
      4. Nur mit -EnableBranchPolicy: Branch-Policy für die PR-Validierung
      5. Nur mit -EnableBranchPolicy: Branch-Prüfung an der Azure-Service-Connection, nur Läufe
         von -AllowedBranches (Standard der Default-Branch) mit seed-pipelines aus -TemplateRefs
         (Standard refs/tags/v*) bekommen die Deployment-Identität

    Einstellungen wie in ado-project: Freigabe durch eine der genannten Personen, auch durch
    die, die den Lauf gestartet hat, 30 Tage Zeit; Policy blockierend, Ergebnis 12 Stunden
    gültig, neuer PR-Lauf nur bei Änderungen am PR.

    Vorher: Initialize-SeedTenant.ps1 mit -SkipScaffold für das Azure-DevOps-Projekt (Service
    Connections). Ausführen mit einem Konto, das Projektadministrator in Azure DevOps ist und
    die Service Connections verwalten darf.

.PARAMETER SeedProjectName
    Projektname wie in project.yaml und im Parameter project von web-app.yml, Präfix der
    Environments. 3-20 Zeichen, Kleinbuchstaben, Ziffern und Bindestriche.

.PARAMETER Environments
    Umgebungen in Deploy-Reihenfolge, z. B. test,prod.

.PARAMETER Approvers
    Principal Names (E-Mail) der Personen, die Infrastruktur-Änderungen freigeben. Leer:
    keine Freigabe.

.PARAMETER AppApprovalEnvironments
    Umgebungen, in denen auch der App-Deploy eine Freigabe braucht, z. B. prod.

.PARAMETER PipelineName
    Name der Pipeline-Definition; ohne Angabe der Seed-Projektname.

.PARAMETER EnableBranchPolicy
    Branch-Policy für die PR-Validierung setzen. Erst nach dem Umschalten: Die Policy ist
    Pflicht und sperrt direkte Pushes. Vorher prüft das Skript, dass die YAML-Datei auf dem
    Default-Branch web-app.yml aus seed-pipelines ab v0.5.0 einbindet; mit der alten Datei
    oder einer älteren Version würden PR-Läufe deployen.

.PARAMETER AllowedBranches
    Branches, deren Läufe die Azure-Service-Connection nutzen dürfen (Check „Branch control“),
    Standard der Default-Branch. Gesetzt wird die Prüfung erst mit -EnableBranchPolicy: Bis zum
    Umschalten testet die Migration auf einem eigenen Branch. Gilt für alle Pipelines, die diese
    Service Connection nutzen. Muster wie refs/heads/release/* sind erlaubt.

.PARAMETER TemplateRefs
    Refs von seed-pipelines, die die Branch-Prüfung zusätzlich erlaubt, Standard refs/tags/v*.
    Die Prüfung vergleicht jedes Repo des Laufs mit derselben Liste, auch das Template-Repo, das
    Projekte über einen Tag einbinden. Ein Tag v… im Projekt-Repo und ein Lauf darauf kämen
    deshalb ebenfalls durch: Im Projekt-Repo sollten nur Administratoren Tags anlegen dürfen
    (README, Branch-Prüfung).

.PARAMETER DryRun
    Nur prüfen und anzeigen, was angelegt würde. Ändert nichts.

.EXAMPLE
    ./Initialize-SeedProject.ps1 -AzureDevOpsOrganization https://dev.azure.com/kunde -AzureDevOpsProject Apps `
        -Repository Kundenportal -SeedProjectName kundenportal -Environments test,prod `
        -Approvers it@kunde.de -AppApprovalEnvironments prod -ServiceConnectionName sc-kundenportal -DryRun
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AzureDevOpsOrganization,
    [Parameter(Mandatory)] [string] $AzureDevOpsProject,
    [Parameter(Mandatory)] [string] $Repository,
    [Parameter(Mandatory)] [string] $SeedProjectName,
    [string[]] $Environments = @('dev'),
    [string[]] $Approvers = @(),
    [string[]] $AppApprovalEnvironments = @(),
    [string] $ServiceConnectionName = 'sc-seed',
    [string] $GitHubConnectionName = 'github-blackforestsentinel',
    [string] $YamlPath = 'azure-pipelines.yml',
    [string] $PipelineName = '',
    [string] $DefaultBranch = 'main',
    [switch] $EnableBranchPolicy,
    [string[]] $AllowedBranches = @(),
    [string[]] $TemplateRefs = @('refs/tags/v*'),
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

# Listen auch als "test,prod" in einem Argument annehmen, etwa beim Aufruf über pwsh -File.
function Split-List([string[]] $Values) {
    @($Values | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
$Environments = Split-List $Environments
$Approvers = Split-List $Approvers
$AppApprovalEnvironments = Split-List $AppApprovalEnvironments
$AllowedBranches = Split-List $AllowedBranches
if (-not $AllowedBranches) { $AllowedBranches = @($DefaultBranch) }
$AllowedBranchRefs = @(@($AllowedBranches | ForEach-Object { if ($_ -like 'refs/*') { $_ } else { "refs/heads/$_" } }) +
    @(Split-List $TemplateRefs)) -join ','

if ($SeedProjectName -cnotmatch '^[a-z][a-z0-9-]{1,18}[a-z0-9]$') {
    throw 'SeedProjectName: 3-20 Zeichen, Kleinbuchstaben, Ziffern und Bindestriche, beginnt mit einem Buchstaben.'
}
if (-not $Environments) { throw 'Environments: mindestens eine Umgebung.' }
# ado-project übergeht unbekannte Umgebungen; hier wäre das meist ein Tippfehler und die Freigabe fehlte.
$unknown = $AppApprovalEnvironments | Where-Object { $_ -notin $Environments }
if ($unknown) { throw "AppApprovalEnvironments: $($unknown -join ', ') steht nicht in Environments." }
if (-not $PipelineName) { $PipelineName = $SeedProjectName }

$Org = $AzureDevOpsOrganization.TrimEnd('/')
$orgName = if ($Org -match '^https://dev\.azure\.com/([^/]+)$') { $Matches[1] }
    elseif ($Org -match '^https://([^./]+)\.visualstudio\.com$') { $Matches[1] }
    else { throw 'AzureDevOpsOrganization: https://dev.azure.com/<organisation> erwartet.' }
$Identities = "https://vssps.dev.azure.com/$orgName/_apis/identities"
$AzureDevOpsResource = '499b84ac-1321-427f-aa17-267ca6975798'
$ApprovalCheckType = '8c6f20a7-a545-4486-9777-f762fafe0d4d'
$BuildPolicyType = '0609b952-1397-4640-95ec-e00a01b2c241'
$BranchControlCheckType = 'fe1de3ee-a436-41b4-bb20-f6eb4cb879a7'
$BranchControlDefinition = '86b05a0c-73e6-4f7d-b3cf-e38f3b39a75b'
$BranchRef = "refs/heads/$DefaultBranch"

$work = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) "seed-project-$(Get-Random)")
$missing = [Collections.Generic.List[string]]::new()
$token = $null

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

# Für Prüfungen: Fehler (etwa 404 für eine fehlende Ressource) bedeuten "nicht vorhanden".
function Get-OrNull([scriptblock] $Command) {
    try { & $Command } catch { $null }
}

# Wie die Vorbedingung in ado-project: PR-Läufe ohne Deploy kennt web-app.yml erst ab
# seed-pipelines v0.5.0; Branches (Tests) gelten als neu. Liefert den Grund oder nichts.
function Test-SeedYaml([string] $Content) {
    if (-not $Content) { return 'Datei fehlt' }
    if ($Content -notmatch 'templates/web-app\.yml@') { return 'bindet templates/web-app.yml aus seed-pipelines nicht ein' }
    if ($Content -match 'refs/tags/v(\d+)\.(\d+)\.(\d+)') {
        $version = [version]::new([int] $Matches[1], [int] $Matches[2], [int] $Matches[3])
        if ($version -lt [version] '0.5.0') { return "seed-pipelines v$version, PR-Läufe ohne Deploy erst ab v0.5.0" }
    }
}

try {
    $project = Invoke-AzDevOps get "$Org/_apis/projects/$([uri]::EscapeDataString($AzureDevOpsProject))?api-version=7.1"
    $api = "$Org/$($project.id)/_apis"
    $repo = Get-OrNull { Invoke-AzDevOps get "$api/git/repositories/$([uri]::EscapeDataString($Repository))?api-version=7.1" }
    if (-not $repo) { throw "Repo $Repository gibt es im Projekt $($project.name) nicht." }
    $branch = (Invoke-AzDevOps get "$api/git/repositories/$($repo.id)/refs?filter=$([uri]::EscapeDataString("heads/$DefaultBranch"))&api-version=7.1").value |
        Where-Object name -eq $BranchRef
    if (-not $branch) { throw "Branch $DefaultBranch gibt es im Repo $($repo.name) nicht." }

    # Wie data.azuredevops_users in ado-project: genau eine aktive Person je Adresse.
    $approverIds = @(foreach ($mail in $Approvers) {
            $found = @((Invoke-AzDevOps get "$Identities`?searchFilter=General&filterValue=$([uri]::EscapeDataString($mail))&queryMembership=None&api-version=7.1").value |
                    Where-Object { $_.isActive -and ($_.properties.Account.'$value' -eq $mail -or $_.properties.Mail.'$value' -eq $mail) })
            if ($found.Count -ne 1) { throw "Freigabe: Zu $mail gibt es $($found.Count) Personen in Azure DevOps, erwartet ist genau eine." }
            $found[0].id
        })

    Write-Host "Sentinel Seed Projekt-Einrichtung$(if ($DryRun) { ' (DryRun, keine Änderungen)' })"
    Write-Host "  Azure DevOps $Org/$($project.name), Repo $($repo.name), Branch $DefaultBranch, Pipeline $PipelineName ($YamlPath)"
    Write-Host ("  Seed-Projekt $SeedProjectName, Umgebungen $($Environments -join ', '), Freigabe $(if ($Approvers) { $Approvers -join ', ' } else { 'keine' })" +
        $(if ($AppApprovalEnvironments) { ", App-Freigabe in $($AppApprovalEnvironments -join ', ')" }))
    Write-Host ''

    # --- 1. Environments und Freigaben ----------------------------------------------------
    Write-Host '1. Environments und Freigaben'
    # Je Umgebung wie in ado-project: <projekt>-<umgebung> für Infrastruktur (terraform apply),
    # <projekt>-<umgebung>-app für den App-Deploy.
    $specs = foreach ($e in $Environments) {
        [pscustomobject] @{ Name = "$SeedProjectName-$e"; Description = "Infrastruktur ${e}: terraform apply"; Approval = $approverIds.Count -gt 0 }
        [pscustomobject] @{ Name = "$SeedProjectName-$e-app"; Description = "App-Deploy $e"; Approval = $approverIds.Count -gt 0 -and $e -in $AppApprovalEnvironments }
    }
    $environmentIds = [ordered] @{}
    foreach ($spec in $specs) {
        $environment = Invoke-Step "Environment $($spec.Name)" {
            (Invoke-AzDevOps get "$api/distributedtask/environments?name=$([uri]::EscapeDataString($spec.Name))&api-version=7.1").value |
                Where-Object name -eq $spec.Name | Select-Object -First 1
        } {
            Invoke-AzDevOps post "$api/distributedtask/environments?api-version=7.1" @{ name = $spec.Name; description = $spec.Description }
        }
        $environmentIds[$spec.Name] = if ($environment) { $environment.id }
        if (-not $spec.Approval) { continue }
        Invoke-Step "Freigabe an $($spec.Name)" {
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
                    instructions              = 'Plan im Log der Stage Plan prüfen, dann freigeben.'
                    blockedApprovers          = @()
                }
                timeout  = 43200
            }
        } | Out-Null
    }

    # --- 2. Pipeline ------------------------------------------------------------------------
    Write-Host '2. Pipeline'
    $definition = Invoke-Step "Pipeline $PipelineName ($YamlPath)" {
        $reference = (Invoke-AzDevOps get "$api/build/definitions?name=$([uri]::EscapeDataString($PipelineName))&api-version=7.1").value |
            Where-Object name -eq $PipelineName | Select-Object -First 1
        if ($reference) {
            $existing = Invoke-AzDevOps get "$api/build/definitions/$($reference.id)?api-version=7.1"
            if ($existing.repository.id -ne $repo.id -or "$($existing.process.yamlFilename)".TrimStart('/') -ne $YamlPath.TrimStart('/')) {
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
            # Wie ci_trigger { use_yaml = true }: Die Trigger stehen in der YAML-Datei.
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

    # Übergang: Solange auf dem Default-Branch die alte YAML-Datei liegt, startet ein Push dort
    # auch die neue Pipeline, mit dem alten Inhalt. Andere Pipelines des Repos laufen nach dem
    # Merge mit der neuen Datei weiter, wenn sie aktiv bleiben.
    $yaml = Get-OrNull {
        (Invoke-AzDevOps get "$api/git/repositories/$($repo.id)/items?path=$([uri]::EscapeDataString($YamlPath))&versionDescriptor.version=$([uri]::EscapeDataString($DefaultBranch))&versionDescriptor.versionType=branch&includeContent=true&`$format=json&api-version=7.1").content
    }
    $seedYamlProblem = Test-SeedYaml $yaml
    if (-not $yaml) {
        Write-Host "          $YamlPath fehlt auf ${DefaultBranch}: Die Pipeline läuft dort erst nach dem Merge."
    } elseif ($seedYamlProblem) {
        Write-Host "          $YamlPath auf $DefaultBranch ist noch nicht umgestellt ($seedYamlProblem)." -ForegroundColor Yellow
        Write-Host "          Bis zum Merge startet ein Push auf $DefaultBranch auch diese Pipeline mit dem alten Inhalt."
        Write-Host '          Fragt ein solcher Lauf nach Zugriff auf alte Service Connections oder Environments:'
        Write-Host '          nicht erlauben, sondern abbrechen.'
    }
    $others = @((Invoke-AzDevOps get "$api/build/definitions?repositoryId=$($repo.id)&repositoryType=TfsGit&includeAllProperties=true&api-version=7.1").value |
            Where-Object { $_.id -ne $definition.id -and $_.queueStatus -eq 'enabled' })
    if ($others) {
        Write-Host "          Weitere aktive Pipelines auf $($repo.name), vor dem Merge deaktivieren:" -ForegroundColor Yellow
        $others | ForEach-Object { Write-Host "            $($_.name) (ID $($_.id), $($_.process.yamlFilename))" }
    }

    # --- 3. Berechtigungen ------------------------------------------------------------------
    Write-Host '3. Berechtigungen der Pipeline'
    function Get-ServiceEndpoint([string] $Name) {
        (Invoke-AzDevOps get "$api/serviceendpoint/endpoints?endpointNames=$([uri]::EscapeDataString($Name))&api-version=7.1").value |
            Where-Object name -eq $Name | Select-Object -First 1
    }
    $azure = Get-ServiceEndpoint $ServiceConnectionName
    $github = Get-ServiceEndpoint $GitHubConnectionName
    $resources = @(
        foreach ($name in $environmentIds.Keys) { @{ Title = "Environment $name"; Type = 'environment'; Id = $environmentIds[$name] } }
        @{ Title = "Service Connection $ServiceConnectionName"; Type = 'endpoint'; Id = $azure.id; Reason = 'Service Connection fehlt im Projekt: zuerst Initialize-SeedTenant.ps1 ausführen.' }
        @{ Title = "GitHub-Service-Connection $GitHubConnectionName"; Type = 'endpoint'; Id = $github.id; Reason = 'GitHub-Service-Connection fehlt im Projekt: zuerst Initialize-SeedTenant.ps1 ausführen.' }
    )
    foreach ($resource in $resources) {
        $title = "Pipeline berechtigt für $($resource.Title)"
        if (-not $resource.Id) {
            Write-Missing $title $resource.Reason
            continue
        }
        $permissionsUrl = "$api/pipelines/pipelinePermissions/$($resource.Type)/$($resource.Id)?api-version=7.1-preview.1"
        Invoke-Step $title {
            if ($definition) {
                (Invoke-AzDevOps get $permissionsUrl).pipelines | Where-Object { $_.id -eq $definition.id -and $_.authorized }
            }
        } {
            Invoke-AzDevOps patch $permissionsUrl @{ pipelines = @(@{ id = $definition.id; authorized = $true }) } | Out-Null
            $true
        } | Out-Null
    }

    # --- 4. Branch-Policy -------------------------------------------------------------------
    # Pull Requests brauchen einen erfolgreichen PR-Lauf der Pipeline. Als Pflicht-Policy sperrt
    # sie direkte Pushes; deshalb erst nach dem Umschalten und nur mit -EnableBranchPolicy.
    Write-Host '4. Branch-Policy PR-Validierung'
    $policyTitle = "PR-Validierung auf $DefaultBranch (Pipeline $PipelineName)"
    $policy = if ($definition) {
        (Invoke-AzDevOps get "$api/policy/configurations?policyType=$BuildPolicyType&api-version=7.1").value | Where-Object {
            $_.type.id -eq $BuildPolicyType -and $_.settings.buildDefinitionId -eq $definition.id -and
            ($_.settings.scope | Where-Object { $_.repositoryId -eq $repo.id -and $_.refName -eq $BranchRef })
        } | Select-Object -First 1
    }
    if ($policy) {
        Write-Host "  [ok]    $policyTitle"
    } elseif (-not $EnableBranchPolicy) {
        Write-Host "          entfällt: erst nach dem Umschalten von $DefaultBranch, mit -EnableBranchPolicy"
    } elseif ($seedYamlProblem) {
        Write-Missing $policyTitle "$YamlPath auf ${DefaultBranch}: $seedYamlProblem. Mit dieser Datei würden PR-Läufe deployen."
    } else {
        Invoke-Step $policyTitle { $null } {
            Invoke-AzDevOps post "$api/policy/configurations?api-version=7.1" @{
                isEnabled  = $true
                isBlocking = $true
                type       = @{ id = $BuildPolicyType }
                settings   = @{
                    buildDefinitionId       = $definition.id
                    displayName             = 'PR-Validierung'
                    manualQueueOnly         = $false
                    # Ein Ergebnis gilt 12 Stunden, auch wenn sich der Branch ändert: Mit einem
                    # parallelen Job soll nicht jeder Merge alle offenen PRs neu bauen.
                    queueOnSourceUpdateOnly = $true
                    validDuration           = 720
                    scope                   = @(@{ repositoryId = $repo.id; refName = $BranchRef; matchKind = 'Exact' })
                }
            } | Out-Null
            $true
        } | Out-Null
    }

    # --- 5. Branch-Prüfung ------------------------------------------------------------------
    # Freigaben hängen an den Environments. Eine geänderte Pipeline-Datei auf einem Feature-Branch,
    # die die Service Connection direkt nutzt, hielten sie nicht auf; die Branch-Prüfung schon.
    # Wie die Branch-Policy erst nach dem Umschalten, weil die Migration vorher auf einem Branch
    # testet. Eine vorhandene Prüfung mit anderen Branches wird aktualisiert.
    Write-Host '5. Branch-Prüfung an der Azure-Service-Connection'
    $branchTitle = "Branch-Prüfung an ${ServiceConnectionName}: $AllowedBranchRefs"
    if (-not $EnableBranchPolicy) {
        Write-Host "          entfällt: erst nach dem Umschalten von $DefaultBranch, mit -EnableBranchPolicy"
    } elseif (-not $azure) {
        Write-Missing $branchTitle 'Service Connection fehlt im Projekt: zuerst Initialize-SeedTenant.ps1 ausführen.'
    } else {
        $checks = "$api/pipelines/checks/configurations"
        $existing = (Invoke-AzDevOps get "$checks`?resourceType=endpoint&resourceId=$($azure.id)&`$expand=settings&api-version=7.1-preview.1").value |
            Where-Object { $_.type.id -eq $BranchControlCheckType -and $_.settings.definitionRef.id -eq $BranchControlDefinition } |
            Select-Object -First 1
        $body = @{
            type     = @{ id = $BranchControlCheckType; name = 'Task Check' }
            resource = @{ type = 'endpoint'; id = "$($azure.id)"; name = $azure.name }
            settings = @{
                definitionRef = @{ id = $BranchControlDefinition; name = 'evaluatebranchProtection'; version = '0.0.1' }
                displayName   = 'Sentinel Seed: nur freigegebene Branches'
                inputs        = @{ allowedBranches = $AllowedBranchRefs; ensureProtectionOfBranch = 'false'; allowUnknownStatusBranch = 'false' }
            }
            timeout  = 1440
        }
        if ($existing -and $existing.settings.inputs.allowedBranches -ne $AllowedBranchRefs) {
            if ($DryRun) {
                Write-Host "  [alt]   $branchTitle (bisher $($existing.settings.inputs.allowedBranches))" -ForegroundColor Yellow
                $missing.Add($branchTitle)
            } else {
                $body.id = $existing.id
                Invoke-AzDevOps patch "$checks/$($existing.id)?api-version=7.1-preview.1" $body | Out-Null
                Write-Host "  [neu]   $branchTitle (bisher $($existing.settings.inputs.allowedBranches))" -ForegroundColor Green
            }
        } else {
            Invoke-Step $branchTitle { $existing } {
                Invoke-AzDevOps post "$checks`?api-version=7.1-preview.1" $body
            } | Out-Null
        }
    }

    # --- Ergebnis ---------------------------------------------------------------------------
    Write-Host ''
    if ($missing.Count) {
        Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" }
    } else {
        Write-Host 'Fertig.' -ForegroundColor Green
    }
    Write-Host 'Werte für azure-pipelines.yml des Projekts:'
    Write-Host "  project: $SeedProjectName, serviceConnection: $ServiceConnectionName, endpoint: $GitHubConnectionName"
    Write-Host "  environments: $($Environments -join ', ')"
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
