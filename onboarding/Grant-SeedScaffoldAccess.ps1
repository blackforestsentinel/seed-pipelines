#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: zentrales Scaffold in einem Azure-DevOps-Projekt freischalten (einmal je Projekt).

.DESCRIPTION
    Das zentrale Scaffold (seed-scaffold mit -CentralScaffold aus Initialize-SeedTenant.ps1) legt
    Seed-Projekte mit seinem Job-Token an. Dahinter steht die Build-Service-Identität des
    zentralen Projekts, etwa "Sentinel-Seed Build Service (org)". Sie braucht keine Lizenz, aber
    im Zielprojekt Rechte; dieses Skript gibt sie ihr, über eine eigene Gruppe statt über
    Project Administrators. Jeder Schritt prüft zuerst und legt nur an, was fehlt.

      1. Gruppe "Sentinel Seed Scaffold" im Projekt, Mitglied: die Build-Service-Identität;
         Recht View project-level information (ohne es sieht ein Token aus einem anderen
         Projekt hier gar nichts)
      2. Git (alle Repos des Projekts): Read, Contribute, Create branch, Edit policies
      3. Pipelines (alle des Projekts): View, Create und Edit build pipeline, Queue builds;
         Agent-Queues des Projekts: Rolle User (eine neue Pipeline-Definition braucht "Use" an
         ihrer Standard-Queue)
      4. Environments: Rolle Creator (wer ein Environment anlegt, wird dessen Administrator)
      5. Service Connections: Rolle Creator (nötig, um Service Connections ins Projekt zu teilen;
         wer teilt, wird dort Administrator der Service Connection)
      6. Mit -AllowContributorsToCreateRepositories: Contributors dürfen Repos anlegen. Für neue
         Seed-Projekte legt die Person das Repo selbst an; das Scaffold darf es nicht.
      7. Mit -AgentPool: die Gruppe wird Administrator der Agent-Queue, damit das Scaffold neue
         Pipelines für einen eigenen Pool berechtigen kann
      8. Tags im Projekt nur für Administratoren: Deny "Create tag" für Contributors, Build
         Administrators und die Build-Service-Identitäten (nicht mit -AllowTagCreation). Die
         Branch-Prüfung der geteilten Service Connection lässt refs/tags/v* für jedes Repo des Laufs
         durch; ein Tag v… im Repo eines Seed-Projekts bekäme sonst die Deployment-Identität.

    Repos legt die Identität nicht an, und sie sieht nur die Repos, die ein Lauf des Scaffolds
    auscheckt ("Protect access to repositories in YAML pipelines"). Weitere Rechte, etwa
    Repos löschen oder Berechtigungen ändern, bekommt sie nicht.

    Ausführen mit einem Konto, das Projektadministrator im Zielprojekt ist.

.PARAMETER AzureDevOpsProject
    Zielprojekt, das freigeschaltet wird.

.PARAMETER CentralProject
    Name des Azure-DevOps-Projekts mit dem zentralen Scaffold.

.PARAMETER AllowTagCreation
    Tags im Projekt nicht auf Administratoren beschränken (Schritt 8 entfällt, mit Warnung), etwa
    weil andere Repos dort Tags brauchen. Dann sollte ein Admin "Create tag" in jedem Repo eines
    Seed-Projekts selbst verbieten.

.PARAMETER Check
    Nur prüfen und anzeigen, was fehlt. Ändert nichts.

.EXAMPLE
    ./Grant-SeedScaffoldAccess.ps1 -AzureDevOpsOrganization https://dev.azure.com/contoso `
        -AzureDevOpsProject Projekt-0042 -CentralProject Sentinel-Seed -AllowContributorsToCreateRepositories -Check
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $AzureDevOpsOrganization,
    [Parameter(Mandatory)] [string] $AzureDevOpsProject,
    [Parameter(Mandatory)] [string] $CentralProject,
    [string] $GroupName = 'Sentinel Seed Scaffold',
    [switch] $AllowContributorsToCreateRepositories,
    [string] $AgentPool = '',
    [switch] $AllowTagCreation,
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$Org = $AzureDevOpsOrganization.TrimEnd('/')
$orgName = $Org.Split('/')[-1]
$Vssps = "https://vssps.dev.azure.com/$orgName"
$AzureDevOpsResource = '499b84ac-1321-427f-aa17-267ca6975798'
$GitNamespace = '2e9eb7ed-3c0a-47d4-87c1-0ffdd275fd87'
$BuildNamespace = '33344d9c-fc72-4d6f-aba5-fa317101a7e9'
$ProjectNamespace = '52d39943-cb85-4d7f-8fa8-c6baac873819'
$missing = [Collections.Generic.List[string]]::new()

function Get-AuthHeader {
    if (-not $script:token) {
        $script:token = az account get-access-token --resource $AzureDevOpsResource --query accessToken -o tsv
    }
    @{ Authorization = "Bearer $script:token" }
}

function Invoke-AzDevOps([string] $Method, [string] $Url, [object] $Body) {
    $request = @{ Method = $Method; Uri = $Url; Headers = (Get-AuthHeader); ContentType = 'application/json; charset=utf-8' }
    if ($null -ne $Body) { $request.Body = ConvertTo-Json -InputObject $Body -Depth 20 }
    Invoke-RestMethod @request
}

function Get-OrNull([scriptblock] $Command) {
    try { & $Command } catch { $null }
}

# Führt $Create nur aus, wenn $Exists nichts liefert; mit -Check wird nur gemeldet.
function Invoke-Step([string] $Title, [scriptblock] $Exists, [scriptblock] $Create) {
    $found = & $Exists
    if ($found) {
        Write-Host "  [ok]    $Title"
        return $found
    }
    if ($Check) {
        Write-Host "  [fehlt] $Title" -ForegroundColor Yellow
        $missing.Add($Title)
        return $null
    }
    $result = & $Create
    Write-Host "  [neu]   $Title" -ForegroundColor Green
    return $result
}

function Find-BuildIdentity([string] $DisplayName) {
    $continuation = $null
    do {
        $url = "$Vssps/_apis/graph/users?subjectTypes=svc&api-version=7.1-preview.1"
        if ($continuation) { $url += "&continuationToken=$([uri]::EscapeDataString($continuation))" }
        $response = Invoke-RestMethod -Uri $url -Headers (Get-AuthHeader) -ResponseHeadersVariable headers
        $match = $response.value | Where-Object displayName -eq $DisplayName | Select-Object -First 1
        if ($match) { return $match }
        $continuation = if ($headers['x-ms-continuationtoken']) { @($headers['x-ms-continuationtoken'])[0] }
    } while ($continuation)
}

# Identitäts-Deskriptor (für Zugriffslisten) und ID (für Rollen) zu einem Graph-Deskriptor
function Get-Identity([string] $GraphDescriptor) {
    $id = (Invoke-AzDevOps get "$Vssps/_apis/graph/storagekeys/$GraphDescriptor`?api-version=7.1-preview.1" $null).value
    (Invoke-AzDevOps get "$Vssps/_apis/identities?identityIds=$id&api-version=7.1" $null).value[0]
}

function Get-ActionBits([string] $Namespace, [string[]] $Names) {
    $actions = (Invoke-AzDevOps get "$Org/_apis/securitynamespaces/$Namespace`?api-version=7.1" $null).value[0].actions
    $bits = 0
    foreach ($name in $Names) {
        $action = $actions | Where-Object name -eq $name
        if (-not $action) { throw "Recht $name im Namespace $Namespace unbekannt." }
        $bits = $bits -bor $action.bit
    }
    $bits
}

# Erlaubt die Rechte $Bits für die Identität auf $SecurityToken; vorhandene Einträge bleiben.
function Set-Allow([string] $Title, [string] $Namespace, [string] $SecurityToken, [object] $Identity, [int] $Bits) {
    Invoke-Step $Title {
        $acl = (Invoke-AzDevOps get "$Org/_apis/accesscontrollists/$Namespace`?token=$([uri]::EscapeDataString($SecurityToken))&descriptors=$([uri]::EscapeDataString($Identity.descriptor))&api-version=7.1" $null).value
        $allow = 0
        foreach ($entry in $acl) { foreach ($ace in $entry.acesDictionary.PSObject.Properties.Value) { $allow = $allow -bor $ace.allow } }
        ($allow -band $Bits) -eq $Bits
    } {
        Invoke-AzDevOps post "$Org/_apis/accesscontrolentries/$Namespace`?api-version=7.1" @{
            token                = $SecurityToken
            merge                = $true
            accessControlEntries = @(@{ descriptor = $Identity.descriptor; allow = $Bits; deny = 0 })
        } | Out-Null
        $true
    } | Out-Null
}

# Verbietet die Rechte $Bits für die Identität auf $SecurityToken; vorhandene Einträge bleiben.
function Set-Deny([string] $Title, [string] $Namespace, [string] $SecurityToken, [object] $Identity, [int] $Bits) {
    Invoke-Step $Title {
        $acl = (Invoke-AzDevOps get "$Org/_apis/accesscontrollists/$Namespace`?token=$([uri]::EscapeDataString($SecurityToken))&descriptors=$([uri]::EscapeDataString($Identity.descriptor))&api-version=7.1" $null).value
        $deny = 0
        foreach ($entry in $acl) { foreach ($ace in $entry.acesDictionary.PSObject.Properties.Value) { $deny = $deny -bor $ace.deny } }
        ($deny -band $Bits) -eq $Bits
    } {
        Invoke-AzDevOps post "$Org/_apis/accesscontrolentries/$Namespace`?api-version=7.1" @{
            token                = $SecurityToken
            merge                = $true
            accessControlEntries = @(@{ descriptor = $Identity.descriptor; allow = 0; deny = $Bits })
        } | Out-Null
        $true
    } | Out-Null
}

function Set-Role([string] $Title, [string] $Scope, [string] $Resource, [string] $IdentityId, [string] $Role) {
    $url = "$Org/_apis/securityroles/scopes/$Scope/roleassignments/resources/$Resource`?api-version=7.1-preview.1"
    Invoke-Step $Title {
        (Invoke-AzDevOps get $url $null).value | Where-Object { $_.identity.id -eq $IdentityId -and $_.role.name -in @($Role, 'Administrator') }
    } {
        Invoke-AzDevOps put $url @(@{ roleName = $Role; userId = $IdentityId }) | Out-Null
        $true
    } | Out-Null
}

$project = Invoke-AzDevOps get "$Org/_apis/projects/$([uri]::EscapeDataString($AzureDevOpsProject))?api-version=7.1" $null
$buildIdentityName = "$CentralProject Build Service ($orgName)"
Write-Host "Sentinel Seed: zentrales Scaffold freischalten$(if ($Check) { ' (nur prüfen)' })"
Write-Host "  Projekt $($project.name), Identität $buildIdentityName"
Write-Host ''

# --- 1. Gruppe und Mitglied ------------------------------------------------------------------
Write-Host '1. Gruppe'
$buildIdentity = Find-BuildIdentity $buildIdentityName
if (-not $buildIdentity) { throw "$buildIdentityName nicht gefunden. Heißt das zentrale Projekt anders (-CentralProject)?" }
$findGroup = {
    (az devops security group list --org $Org --project $project.id -o json | ConvertFrom-Json).graphGroups |
        Where-Object principalName -eq "[$($project.name)]\$GroupName" | Select-Object -First 1
}
$group = Invoke-Step "Gruppe $GroupName" $findGroup {
    az devops security group create --org $Org --project $project.id --name $GroupName `
        --description 'Sentinel Seed: Rechte des zentralen Scaffolds (Grant-SeedScaffoldAccess.ps1)' -o none
    & $findGroup
}
if ($group) {
    Invoke-Step "Mitglied $buildIdentityName" {
        $members = az devops security group membership list --id $group.descriptor --org $Org -o json | ConvertFrom-Json
        $members.PSObject.Properties.Name -contains $buildIdentity.descriptor
    } {
        az devops security group membership add --group-id $group.descriptor --member-id $buildIdentity.descriptor --org $Org -o none
        $true
    } | Out-Null
}

# --- 2.–5. Rechte der Gruppe ----------------------------------------------------------------
if ($group) {
    $groupIdentity = Get-Identity $group.descriptor
    # Ohne dieses Recht sieht das Job-Token aus einem anderen Projekt hier nichts, auch kein Repo
    # mit Leserecht (Microsoft: Zugriff eines Projekts auf ein anderes).
    Write-Host '1b. Projekt'
    Set-Allow 'View project-level information' $ProjectNamespace "`$PROJECT:vstfs:///Classification/TeamProject/$($project.id)" $groupIdentity `
        (Get-ActionBits $ProjectNamespace @('GENERIC_READ'))
    Write-Host '2. Git'
    Set-Allow 'Read, Contribute, Create branch, Edit policies' $GitNamespace "repoV2/$($project.id)" $groupIdentity `
        (Get-ActionBits $GitNamespace @('GenericRead', 'GenericContribute', 'CreateBranch', 'EditPolicies'))
    Write-Host '3. Pipelines'
    Set-Allow 'View, Create und Edit build pipeline, Queue builds' $BuildNamespace $project.id $groupIdentity `
        (Get-ActionBits $BuildNamespace @('ViewBuilds', 'ViewBuildDefinition', 'CreateBuildDefinition', 'EditBuildDefinition', 'QueueBuilds'))
    # Eine Pipeline-Definition anzulegen verlangt "Use" an ihrer Standard-Queue (Azure Pipelines),
    # auch wenn die YAML-Datei einen anderen Pool nennt; vererbt an alle Queues des Projekts.
    Set-Role 'Agent-Queues: Rolle User' 'distributedtask.agentqueuerole' $project.id $groupIdentity.id 'User'
    Write-Host '4. Environments'
    Set-Role 'Rolle Creator' 'distributedtask.globalenvironmentreferencerole' $project.id $groupIdentity.id 'Creator'
    Write-Host '5. Service Connections'
    # Die Rolle Creator gibt es nur im Bereich des Projekts, nicht bei einzelnen Service Connections.
    Set-Role 'Rolle Creator' 'distributedtask.project.serviceendpointrole' $project.id $groupIdentity.id 'Creator'
    if ($AgentPool) {
        Write-Host "7. Agent-Queue $AgentPool"
        $queue = (Invoke-AzDevOps get "$Org/$($project.id)/_apis/distributedtask/queues?queueNames=$([uri]::EscapeDataString($AgentPool))&api-version=7.1" $null).value |
            Select-Object -First 1
        if (-not $queue) { throw "Agent-Queue $AgentPool gibt es im Projekt nicht." }
        Set-Role 'Rolle Administrator' 'distributedtask.agentqueuerole' "$($project.id)_$($queue.id)" $groupIdentity.id 'Administrator'
    }
}

# --- 6. Contributors dürfen Repos anlegen ---------------------------------------------------
if ($AllowContributorsToCreateRepositories) {
    Write-Host '6. Contributors'
    $contributors = (az devops security group list --org $Org --project $project.id -o json | ConvertFrom-Json).graphGroups |
        Where-Object principalName -eq "[$($project.name)]\Contributors" | Select-Object -First 1
    Set-Allow 'Create repository für Contributors' $GitNamespace "repoV2/$($project.id)" (Get-Identity $contributors.descriptor) `
        (Get-ActionBits $GitNamespace @('CreateRepository'))
}

# --- 8. Tags nur für Administratoren ---------------------------------------------------------
# Auch die Job-Tokens der Pipelines im Projekt (Build-Service-Identitäten) könnten Tags anlegen.
Write-Host '8. Tags im Projekt nur für Administratoren'
if ($AllowTagCreation) {
    Write-Host "  [warn]  -AllowTagCreation: In $($project.name) darf ein Admin dann Create tag je Seed-Repo verbieten." -ForegroundColor Yellow
} else {
    $projectGroups = (az devops security group list --org $Org --project $project.id -o json | ConvertFrom-Json).graphGroups
    $tagBits = Get-ActionBits $GitNamespace @('CreateTag')
    foreach ($name in @('Contributors', 'Build Administrators')) {
        $projectGroup = $projectGroups | Where-Object principalName -eq "[$($project.name)]\$name" | Select-Object -First 1
        if (-not $projectGroup) { throw "Gruppe [$($project.name)]\$name nicht gefunden." }
        Set-Deny "Create tag verboten für $name" $GitNamespace "repoV2/$($project.id)" (Get-Identity $projectGroup.descriptor) $tagBits
    }
    foreach ($name in @("$($project.name) Build Service ($orgName)", "Project Collection Build Service ($orgName)")) {
        $service = Find-BuildIdentity $name
        if (-not $service) { throw "$name nicht gefunden." }
        Set-Deny "Create tag verboten für $name" $GitNamespace "repoV2/$($project.id)" (Get-Identity $service.descriptor) $tagBits
    }
}

Write-Host ''
if ($missing.Count) {
    Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
    $missing | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
Write-Host "Fertig: Das zentrale Scaffold darf in $($project.name) Seed-Projekte anlegen." -ForegroundColor Green
