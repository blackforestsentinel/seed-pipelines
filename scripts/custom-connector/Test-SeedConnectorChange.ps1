#Requires -Version 7.2
<#
.SYNOPSIS
    Prüft vor dem Update eines Custom Connectors, ob die neue OpenAPI-Definition Flows bricht, die den
    Connector nutzen, und auf Wunsch, ob die Konten seiner Verbindungen eine App-Rolle haben.

.DESCRIPTION
    Teil von templates/steps/custom-connector-check.yml (Job connector_check in plan_<env>, vor jeder
    Freigabe). Vergleicht die openapidefinition des bestehenden Connectors (Dataverse, Tabelle
    connector) mit dem openapi.json aus dem Build und gleicht jede Änderung mit den Flows ab, die den
    Connector nutzen (Tabelle workflow, Spalte clientdata). README, Prüfung des Custom Connectors.

    Ergebnis:
      - Fehler, wenn ein aktiver oder angehaltener Flow eine entfernte Operation nutzt oder eine
        Änderung ihn bricht (neues Pflichtfeld, anderer Typ, nicht mehr erlaubter Wert); für
        ausgeschaltete Flows und Entwürfe eine Warnung
      - Warnung je Flow für deprecated Operationen, entfallene Eingabefelder und Antwortfelder, die
        der Flow per Ausdruck liest
      - Warnung „Nutzung nicht prüfbar“, wenn die Identität Flows nicht auf Ebene Global liest
      - mit -CheckConnections je Verbindung ohne App-Rolle eine Warnung
    Ausgabe als ##vso[task.logissue] und als Markdown-Zusammenfassung (##vso[task.uploadsummary]).
    Exit 1 bei Fehlern; -AllowBreakingChanges macht alle Fehler zu Warnungen.

    Token wie in custom-connector.yml aus az account get-access-token (AzureCLI@2). Das Skript liest
    nur, es ändert nichts.

.PARAMETER EnvironmentUrl
    Umgebungs-URL der Power-Platform-Umgebung (Output custom_connector.environmentUrl).

.PARAMETER Name
    Name des Connectors ohne Präfix des Standard-Publishers (Output custom_connector.name); den
    Präfix ermittelt das Skript wie custom-connector.yml.

.PARAMETER OpenApiFile
    openapi.json aus dem Build (Artefakt openapi).

.PARAMETER SummaryFile
    Ziel der Markdown-Zusammenfassung.

.PARAMETER AllowBreakingChanges
    Alle Fehler werden zu Warnungen (Parameter allowBreakingConnectorChanges in web-app.yml).

.PARAMETER CheckConnections
    Verbindungen zum Connector und die App-Rollen ihrer Konten prüfen (Parameter
    checkConnectorConnections). Braucht den Admin-Scope der Power Apps API und Graph-Rechte; fehlen
    sie, gibt es die Warnung „Verbindungen nicht prüfbar“.

.EXAMPLE
    ./Test-SeedConnectorChange.ps1 -EnvironmentUrl https://org0000000.crm4.dynamics.com -Name ordersdev `
        -OpenApiFile ./openapi.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $EnvironmentUrl,
    [Parameter(Mandatory)] [string] $Name,
    [Parameter(Mandatory)] [string] $OpenApiFile,
    [string] $SummaryFile = (Join-Path ([IO.Path]::GetTempPath()) 'custom-connector-check.md'),
    [switch] $AllowBreakingChanges,
    [switch] $CheckConnections
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
Import-Module (Join-Path $PSScriptRoot 'SeedConnectorCheck.psm1') -Force

$findings = [Collections.Generic.List[object]]::new()
$usages = @()
$connections = @()
$oldOperations = [ordered]@{}
$newOperations = [ordered]@{}
$states = [ordered]@{}
$row = $null
$environmentId = ''
$backend = ''
$flowCheck = 'nicht gelaufen'
$connectionCheck = if ($CheckConnections) { 'nicht gelaufen' } else { 'aus (Parameter checkConnectorConnections)' }

function Get-Token([string] $Resource) { az account get-access-token --resource $Resource --query accessToken -o tsv }

function Invoke-PagedGet($Request, [string] $Url, [string] $NextLink = '@odata.nextLink') {
    while ($Url) {
        $page = Invoke-RestMethod @Request -Uri $Url
        $page.value
        $Url = $page.$NextLink
    }
}

function Get-Message($ErrorRecord) {
    $detail = $ErrorRecord.ErrorDetails.Message
    $message = try { (ConvertFrom-Json $detail).error.message } catch { $null }
    $text = "$($ErrorRecord.Exception.Message)$(if ($message) { " $message" })"
    if ($text.Length -gt 300) { $text = $text.Substring(0, 300) + '…' }
    $text
}

# Tiefe eines Leserechts der angemeldeten Identität; nur Global sieht die Datensätze aller Konten.
function Get-PrivilegeDepth($Dataverse, [string] $Api, [string] $UserId, [string] $Privilege) {
    try {
        $depth = @((Invoke-RestMethod @Dataverse "$Api/systemusers($UserId)/Microsoft.Dynamics.CRM.RetrieveUserPrivilegeByPrivilegeName(PrivilegeName='$Privilege')").RolePrivileges.Depth)
        [pscustomobject]@{ Global = $depth -contains 'Global'; Depth = if ($depth.Count) { $depth -join ', ' } else { 'keine' } }
    }
    catch {
        [pscustomobject]@{ Global = $false; Depth = "nicht ermittelbar ($(Get-Message $_))" }
    }
}

# Verbindungen zum Connector (Power Apps API, Admin-Scope) mit Konto, Status und App-Rollen (Graph).
# Das Konto ist authenticatedUser, sonst accountName, sonst die Person, die die Verbindung angelegt hat;
# Letztere muss nicht das angemeldete Konto sein.
function Get-ConnectionRows($Dataverse, [string] $Api, [string] $UserId, [string] $EnvironmentId, [string] $InternalId, [string] $ResourceUri) {
    $powerApps = @{ Headers = @{ Authorization = "Bearer $(Get-Token 'https://service.powerapps.com/')" } }
    $url = "https://api.powerapps.com/providers/Microsoft.PowerApps/scopes/admin/environments/$EnvironmentId/connections?api-version=2016-11-01"
    $rows = @(Invoke-PagedGet $powerApps $url 'nextLink' | Where-Object { [string] $_.properties.apiId -like "*/$InternalId" })

    # Genutzt heißt: eine Connection Reference zeigt auf die Verbindung. Ohne Leserecht auf Ebene
    # Global bleibt das unbekannt, statt still „nein“ zu sagen.
    $used = $null
    if ((Get-PrivilegeDepth $Dataverse $Api $UserId 'prvReadconnectionreference').Global) {
        $used = @(Invoke-PagedGet $Dataverse "$Api/connectionreferences?`$select=connectionid&`$filter=contains(connectorid,'$InternalId')" | ForEach-Object connectionid)
    }

    $graph = 'https://graph.microsoft.com/v1.0'
    $graphRequest = @{ Headers = @{ Authorization = "Bearer $(Get-Token 'https://graph.microsoft.com')" } }
    $filter = [uri]::EscapeDataString("servicePrincipalNames/any(x:x eq '$($ResourceUri.Replace("'", "''"))')")
    $sp = @(Invoke-RestMethod @graphRequest "$graph/servicePrincipals?`$filter=$filter&`$select=id,displayName,appRoles").value | Select-Object -First 1
    if (-not $sp) { throw "Keine Unternehmensanwendung zu $ResourceUri gefunden." }
    $roleNames = @{}
    foreach ($r in $sp.appRoles) { $roleNames[[string] $r.id] = $r.value }
    $assignments = @(Invoke-PagedGet $graphRequest "$graph/servicePrincipals/$($sp.id)/appRoleAssignedTo?`$top=999")
    $groupIds = @($assignments | Where-Object principalType -eq 'Group' | ForEach-Object principalId | Select-Object -Unique)

    foreach ($c in $rows) {
        $p = $c.properties
        $account = $p.authenticatedUser.name ?? $p.accountName ?? $p.createdBy.userPrincipalName
        $roles = @()
        if ($account) {
            try {
                $userId = (Invoke-RestMethod @graphRequest "$graph/users/$([uri]::EscapeDataString($account))?`$select=id").id
                $memberOf = @()
                if ($groupIds.Count) {
                    $memberOf = @(Invoke-PagedGet $graphRequest "$graph/users/$userId/transitiveMemberOf/microsoft.graph.group?`$select=id&`$top=999" | ForEach-Object id)
                }
                $roles = @($assignments | Where-Object { $_.principalId -eq $userId -or $_.principalId -in $memberOf } |
                        ForEach-Object { $roleNames[[string] $_.appRoleId] ?? 'Standardzugriff' } | Select-Object -Unique)
            }
            catch { $roles = @('?') }
        }
        [pscustomobject]@{
            Connection = $c.name
            Account    = $account
            Status     = ($p.statuses | Select-Object -First 1).status
            Used       = if ($null -eq $used) { $null } else { $used -contains $c.name }
            Roles      = $roles
            App        = $sp.displayName
        }
    }
}

try {
    $environmentUrl = $EnvironmentUrl.TrimEnd('/')
    # Beide Werte stehen in OData-Filtern und URLs; erlaubt ist nur, was seed-template erzeugt.
    if ($environmentUrl -notmatch '^https://[a-z0-9-]+(\.[a-z0-9-]+)+$') { throw "Ungültige Umgebungs-URL: $environmentUrl" }
    if ($Name -notmatch '^[a-z0-9]+$') { throw "Ungültiger Connector-Name: $Name" }
    if (-not (Test-Path $OpenApiFile)) {
        throw 'features.customConnector braucht das OpenAPI-Dokument aus dem Build: Paket Bfs.Seed.OpenApi im API-Projekt (README von seed-template, customConnector).'
    }
    $newOperations = Get-SeedConnectorOperations (Get-Content -Raw $OpenApiFile | ConvertFrom-Json -AsHashtable)

    $api = "$environmentUrl/api/data/v9.2"
    $dv = @{ Headers = @{
            Authorization = "Bearer $(Get-Token $environmentUrl)"; Accept = 'application/json'
            Prefer        = 'odata.include-annotations="OData.Community.Display.V1.FormattedValue",odata.maxpagesize=50'
        }
    }
    try {
        $userId = (Invoke-RestMethod @dv "$api/WhoAmI").UserId
    }
    catch {
        throw "Kein Zugriff auf $environmentUrl ($(Get-Message $_)). Die Pipeline-Identität braucht dort einen Anwendungsbenutzer mit Environment Maker: onboarding/Initialize-SeedTenant.ps1 mit -PowerPlatformEnvironmentUrl $environmentUrl ausführen."
    }

    # Name wie in custom-connector.yml: Präfix des Standard-Publishers und Name aus dem Output
    $prefix = (Invoke-RestMethod @dv "$api/solutions?`$select=uniquename&`$filter=uniquename eq 'Default'&`$expand=publisherid(`$select=customizationprefix)").value[0].publisherid.customizationprefix
    $connectorName = "$($prefix)_$Name"
    $row = (Invoke-RestMethod @dv "$api/connectors?`$select=connectorid,connectorinternalid,displayname,openapidefinition,connectionparameters&`$filter=name eq '$connectorName'").value |
        Select-Object -First 1
    if (-not $row) {
        Write-Host "Connector $connectorName gibt es in $environmentUrl noch nicht; nichts zu vergleichen."
        exit 0
    }
    $internalId = [string] $row.connectorinternalid
    if ($internalId -notmatch '^[A-Za-z0-9_-]+$') { throw "Unerwartete connectorinternalid: $internalId" }
    $environmentId = [string] (Invoke-RestMethod @dv "$api/RetrieveCurrentOrganization(AccessType=@p)?@p=Microsoft.Dynamics.CRM.EndpointAccessType'Default'").Detail.EnvironmentId
    $oldDocument = $row.openapidefinition | ConvertFrom-Json -AsHashtable
    $oldOperations = Get-SeedConnectorOperations $oldDocument
    if ($oldDocument['host']) { $backend = "https://$($oldDocument['host'])$($oldDocument['basePath'])" }

    # Ohne Leserecht auf Ebene Global sähe die Prüfung nur Flows der Pipeline-Identität und meldete
    # still 0 Treffer.
    $read = Get-PrivilegeDepth $dv $api $userId 'prvReadWorkflow'
    $usageKnown = $read.Global
    if ($usageKnown) {
        $flows = @(Invoke-PagedGet $dv "$api/workflows?`$select=name,workflowid,resourceid,statecode,clientdata,_ownerid_value&`$filter=category eq 5 and contains(clientdata,'$internalId')")
        $usages = @(foreach ($f in $flows) {
                $flow = [pscustomobject]@{
                    Name = $f.name; Id = $f.workflowid; ResourceId = $f.resourceid; StateCode = [int] $f.statecode
                    Owner = $f.'_ownerid_value@OData.Community.Display.V1.FormattedValue'; ClientData = $f.clientdata
                }
                try { Get-SeedFlowUsages $flow $internalId }
                catch {
                    $findings.Add((New-SeedFinding warning '' "Flow nicht auswertbar, Nutzung unbekannt: $(Get-Message $_)" ([pscustomobject]@{
                                    Flow = $flow.Name; FlowId = $flow.Id; ResourceId = $flow.ResourceId; State = Get-SeedFlowState $flow.StateCode; Owner = $flow.Owner
                                })))
                }
            })
        $flowCheck = "$($flows.Count) Flows mit dem Connector, $($usages.Count) Actions"
    }
    else {
        $findings.Add((New-SeedFinding warning '' "Nutzung nicht prüfbar: Die Pipeline-Identität liest Flows auf Ebene $($read.Depth) statt Global. Es fehlt die Rolle Sentinel Seed Flow Reader aus dem Onboarding (onboarding/Initialize-SeedTenant.ps1 mit -PowerPlatformEnvironmentUrl)."))
        $flowCheck = "nicht prüfbar (prvReadWorkflow: $($read.Depth))"
    }
    $result = Compare-SeedConnectorOperations $oldOperations $newOperations $usages $usageKnown
    $states = $result.States
    $findings.AddRange($result.Findings)

    if ($CheckConnections) {
        try {
            $resourceUri = ($row.connectionparameters | ConvertFrom-Json).token.oAuthSettings.properties.AzureActiveDirectoryResourceId
            if (-not $resourceUri) { throw 'Der Connector nennt keine Ressource der Anmeldung (AzureActiveDirectoryResourceId).' }
            if ($environmentId -notmatch '^[A-Za-z0-9-]+$') { throw "Unerwartete Umgebungs-ID: $environmentId" }
            $connections = @(Get-ConnectionRows $dv $api $userId $environmentId $internalId $resourceUri)
            foreach ($f in Get-SeedConnectionFindings $connections ($connections | Select-Object -First 1).App) { $findings.Add($f) }
            $connectionCheck = "$($connections.Count) Verbindungen"
        }
        catch {
            $findings.Add((New-SeedFinding warning '' "Verbindungen nicht prüfbar: $(Get-Message $_)"))
            $connectionCheck = 'nicht prüfbar, Rechte fehlen (README, Prüfung des Custom Connectors)'
        }
    }
}
catch {
    $findings.Add((New-SeedFinding error '' "Prüfung des Custom Connectors gescheitert: $(Get-Message $_)"))
}

if ($AllowBreakingChanges) { Set-SeedOverride $findings }

# --- Log und Zusammenfassung -----------------------------------------------------------------------
foreach ($f in $findings) {
    $where = if ($f.Flow) { " Flow '$($f.Flow)' ($($f.State), Besitzer $($f.Owner)), Action $($f.ActionPath)." } else { '' }
    $text = Format-SeedLogText "$($f.Message)$where"
    if ($f.Severity -eq 'info') { Write-Host $text }
    else { Write-Host "##vso[task.logissue type=$($f.Severity)]$text" }
}

$summary = New-SeedConnectorSummary -DisplayName ($row.displayname ?? $Name) -InternalId $row.connectorinternalid -EnvironmentUrl $EnvironmentUrl `
    -EnvironmentId $environmentId -Backend $backend -States $states -OldOperations $oldOperations -NewOperations $newOperations `
    -Usages $usages -Findings $findings -FlowCheck $flowCheck -ConnectionCheck $connectionCheck -Connections $connections `
    -Overridden ([bool] $AllowBreakingChanges)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SummaryFile) | Out-Null
$summary | Set-Content -Path $SummaryFile -Encoding utf8NoBOM
if ($env:TF_BUILD) { Write-Host "##vso[task.uploadsummary]$SummaryFile" }
else { Write-Host "Zusammenfassung: $SummaryFile" }

$errors = @($findings | Where-Object Severity -eq 'error').Count
$warnings = @($findings | Where-Object Severity -eq 'warning').Count
Write-Host "Custom Connector geprüft: $errors Fehler, $warnings Warnungen."
if ($errors -gt 0) { exit 1 }
exit 0
