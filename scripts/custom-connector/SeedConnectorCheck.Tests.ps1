#Requires -Version 7.2
<#
.SYNOPSIS
    Tests der Connector-Prüfung (SeedConnectorCheck.psm1, Test-SeedConnectorChange.ps1) mit Testdaten
    aus fixtures/, ohne Netzwerk und ohne weitere Module.

.DESCRIPTION
    fixtures/openapi-old.json ist die bisherige Definition, fixtures/openapi-new.json die neue mit
    entfernten und deprecated Operationen, neuem Pflichtfeld, Typänderung, entfallenem Enum-Wert,
    entfallenen Body- und Antwortfeldern. Die Flows (clientdata) rufen die Operationen aus Scopes,
    Schleifen, else, cases und default auf, einer ist ein Entwurf, einer nutzt den Connector nicht.

    Aufruf: pwsh -NoProfile -File scripts/custom-connector/SeedConnectorCheck.Tests.ps1
    Exit 1, wenn ein Test scheitert (GitHub-CI dieses Repos).
#>
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'SeedConnectorCheck.psm1') -Force

$fixtures = Join-Path $PSScriptRoot 'fixtures'
$internalId = 'shared_example-5fordersdev-5f0000000000000000'
$failures = [Collections.Generic.List[string]]::new()
$count = 0

function Test-Case([string] $Name, [scriptblock] $Body) {
    $script:count++
    try {
        & $Body
        Write-Host "  [ok]     $Name"
    }
    catch {
        $script:failures.Add("${Name}: $($_.Exception.Message)")
        Write-Host "  [fehler] ${Name}: $($_.Exception.Message)" -ForegroundColor Red
    }
}

function Assert-Equal($Expected, $Actual, [string] $What) {
    $e = $Expected | ConvertTo-Json -Depth 5 -Compress
    $a = $Actual | ConvertTo-Json -Depth 5 -Compress
    if ($e -cne $a) { throw "$What`n    erwartet: $e`n    erhalten: $a" }
}

function Read-Document([string] $File) { Get-Content -Raw (Join-Path $fixtures $File) | ConvertFrom-Json -AsHashtable }

function New-Flow([string] $File, [string] $Name, [int] $StateCode, [string] $ResourceId = '') {
    [pscustomobject]@{
        Name = $Name; Id = [guid]::NewGuid().ToString(); ResourceId = $ResourceId; StateCode = $StateCode
        Owner = 'Team Vertrieb'; ClientData = Get-Content -Raw (Join-Path $fixtures $File)
    }
}

# Befunde kurz und sortiert: Schwere|Operation|Action|Meldung
function Get-Lines($Findings) {
    @($Findings | ForEach-Object { "$($_.Severity)|$($_.Operation)|$(($_.ActionPath -split '/')[-1])|$($_.Message)" } | Sort-Object)
}

$old = Get-SeedConnectorOperations (Read-Document 'openapi-old.json')
$new = Get-SeedConnectorOperations (Read-Document 'openapi-new.json')
$ordersFlow = New-Flow 'flow-orders.json' 'Bestellungen abgleichen' 1 '11111111-2222-3333-4444-555555555555'
$draftFlow = New-Flow 'flow-draft.json' 'Alte Bestellungen' 0
$usages = @(Get-SeedFlowUsages $ordersFlow $internalId) + @(Get-SeedFlowUsages $draftFlow $internalId)

Write-Host 'OpenAPI'
Test-Case 'Operationen der alten Definition' {
    Assert-Equal @('ListOrders', 'CreateOrder', 'GetOrder', 'CancelOrder', 'ArchiveOrder', 'ListCustomers', 'GetLegacyReport', 'GetHealth') @($old.PSBase.Keys) 'operationIds'
    Assert-Equal 'POST /orders/{id}/cancel' "$($old['CancelOrder'].Method) $($old['CancelOrder'].Path)" 'Methode und Pfad'
}
Test-Case 'Eingabefelder wie in inputs.parameters, Pfadparameter vom Pfad geerbt' {
    Assert-Equal @('body', 'body/customer', 'body/channel', 'body/priority', 'body/note', 'body/lines', 'body/lines[]', 'body/lines[]/sku', 'body/lines[]/quantity') @($old['CreateOrder'].Inputs.PSBase.Keys) 'CreateOrder'
    Assert-Equal @('id') @($old['GetOrder'].Inputs.PSBase.Keys) 'GetOrder'
    Assert-Equal $true $old['GetOrder'].Inputs['id'].Required 'id ist Pflicht'
    Assert-Equal @('Web', 'Phone', 'Mail') $old['CreateOrder'].Inputs['body/channel'].Enum 'Enum'
    Assert-Equal 'body/lines[]' $old['CreateOrder'].Inputs['body/lines[]/sku'].Parent 'Parent'
}
Test-Case 'Antwortfelder, Listen mit []' {
    Assert-Equal @('id', 'state', 'customer', 'customer/name', 'customer/email', 'lines', 'lines[]', 'lines[]/sku', 'lines[]/quantity', 'history', 'history[]', 'history[]/at', 'history[]/note') @($old['GetOrder'].Outputs.PSBase.Keys) 'GetOrder'
    Assert-Equal @() @($old['CancelOrder'].Outputs.PSBase.Keys) 'CancelOrder ohne Antwort'
}
Test-Case 'allOf ergibt dieselben Felder wie ein flaches Schema' {
    Assert-Equal @('id', 'state', 'customer', 'customer/name', 'lines', 'lines[]', 'lines[]/sku', 'lines[]/quantity', 'lines[]/discount') @($new['GetOrder'].Outputs.PSBase.Keys) 'GetOrder neu'
    Assert-Equal $true $new['GetOrder'].Outputs['state'].Required 'required aus allOf'
}
Test-Case 'deprecated' {
    Assert-Equal @('ArchiveOrder', 'GetLegacyReport') @($new.PSBase.Keys | Where-Object { $new[$_].Deprecated }) 'deprecated'
}

Write-Host 'Flows'
Test-Case 'Actions in Scope, Schleife, else, cases und default; andere Connectoren nicht' {
    $paths = @(Get-SeedFlowUsages $ordersFlow $internalId | ForEach-Object { "$($_.OperationId) $($_.ActionPath)" })
    Assert-Equal @(
        'ListOrders /Try/List_orders'
        'CancelOrder /Try/For_each_order/Check_state/Cancel_order'
        'ArchiveOrder /Try/For_each_order/Check_state/else/Archive_order'
        'CreateOrder /Try/Switch_channel/Case_web/Create_web_order'
        'CreateOrder /Try/Switch_channel/default/Create_other_order'
        'GetOrder /Try/Get_order'
    ) $paths 'Treffer'
}
Test-Case 'Do-until und Action nur mit apiId' {
    $paths = @(Get-SeedFlowUsages $draftFlow $internalId | ForEach-Object { "$($_.OperationId) $($_.ActionPath) $($_.State)" })
    Assert-Equal @('CancelOrder /Until_done/Cancel_old_order aus', 'CreateOrder /Create_phone_order aus') $paths 'Treffer'
}
Test-Case 'Flow, der den Connector nur erwähnt' {
    Assert-Equal 0 @(Get-SeedFlowUsages (New-Flow 'flow-other.json' 'Andere' 1) $internalId).Count 'Treffer'
    $read = Read-SeedFlow (New-Flow 'flow-other.json' 'Andere' 1) $internalId
    Assert-Equal 0 @($read.Findings).Count 'keine Warnung, der Flow bindet den Connector nicht ein'
}
Test-Case 'Flow in einer Solution: Connection Reference, keine direkte Verbindung' {
    $read = Read-SeedFlow $ordersFlow $internalId
    Assert-Equal 6 @($read.Usages).Count 'Actions'
    Assert-Equal @() @($read.Connections) 'Verbindungen'
    Assert-Equal 0 @($read.Findings).Count 'Befunde'
}
Test-Case 'Flow außerhalb einer Solution: Verbindung direkt eingebunden' {
    $read = Read-SeedFlow (New-Flow 'flow-personal.json' 'Eigener Flow' 1) $internalId
    Assert-Equal @('GetOrder /Get_order') @($read.Usages | ForEach-Object { "$($_.OperationId) $($_.ActionPath)" }) 'Actions'
    Assert-Equal @('shared-example-5forders-00000000-1111-2222-3333-444444444444') @($read.Connections) 'Verbindungen'
    Assert-Equal 0 @($read.Findings).Count 'Befunde'
}
Test-Case 'Ältere ApiConnection ohne operationId: Warnung statt still 0 Treffer' {
    $read = Read-SeedFlow (New-Flow 'flow-legacy.json' 'Alter Flow' 1) $internalId
    Assert-Equal 0 @($read.Usages).Count 'Actions'
    Assert-Equal @('shared-example-5forders-55555555-6666-7777-8888-999999999999') @($read.Connections) 'Verbindungen'
    Assert-Equal @('warning||Nutzung nicht prüfbar: Der Flow bindet den Connector ein, aber keine Action mit operationId ist erkannt (etwa eine ältere ApiConnection-Action). Bitte von Hand prüfen.') `
    @($read.Findings | ForEach-Object { "$($_.Severity)|$($_.Operation)|$($_.Message)" }) 'Befunde'
    Assert-Equal @('Alter Flow', 'aktiv') @($read.Findings[0].Flow, $read.Findings[0].State) 'Flow im Befund'
}
Test-Case 'Gelesene Antwortfelder: body(), outputs(), items() einer Schleife, Compose mit String-inputs' {
    $byAction = @{}
    foreach ($u in Get-SeedFlowUsages $ordersFlow $internalId) { $byAction[$u.Action] = @($u.OutputPaths | Sort-Object) }
    Assert-Equal @('value/customer/email', 'value/id', 'value/state') $byAction['List_orders'] 'List_orders'
    Assert-Equal @('customer/email', 'history') $byAction['Get_order'] 'Get_order'
    Assert-Equal @() $byAction['Create_web_order'] 'Create_web_order'
}
Test-Case 'Verschachtelte Schleifen' {
    $definition = @{
        actions = @{
            Get_order = @{ type = 'OpenApiConnection' }
            Outer     = @{
                type = 'Foreach'; foreach = "@outputs('Get_order')?['body/lines']"
                actions = @{
                    Inner = @{
                        type = 'Foreach'; foreach = "@items('Outer')?['parts']"
                        actions = @{ Use = @{ type = 'Compose'; inputs = "@items('Inner')?['code']" } }
                    }
                }
            }
        }
    }
    Assert-Equal @('lines/parts/code') @((Get-SeedOutputPaths $definition)['Get_order']) 'Pfad'
}
Test-Case 'Gesetzte Felder: Body als Objekt und Liste aufgefaltet' {
    $fields = Get-SeedSetFields @{ 'body' = @{ customer = 'x'; lines = @(@{ sku = 'A' }) }; 'id' = '@{x}' }
    Assert-Equal 'set' (Get-SeedFieldState $fields 'body/lines[]/sku') 'Listenfeld'
    Assert-Equal 'unset' (Get-SeedFieldState $fields 'body/note') 'nicht gesetzt'
    $byExpression = Get-SeedSetFields @{ 'body' = "@variables('payload')" }
    Assert-Equal 'unknown' (Get-SeedFieldState $byExpression 'body/lines[]/sku') 'per Ausdruck'
}

Write-Host 'Vergleich'
Test-Case 'Unverändertes Dokument: keine Befunde' {
    $result = Compare-SeedConnectorOperations $old $old $usages
    Assert-Equal @() @($result.Findings) 'Befunde'
    Assert-Equal @('unverändert') @($result.States.PSBase.Values | Select-Object -Unique) 'Stand'
}
$expected = @(
    'error|CancelOrder|Cancel_order|Operation CancelOrder entfällt, wird aber noch genutzt.'
    'error|CreateOrder|Create_web_order|Pflichtfeld body/costCenter ist neu; der Flow setzt es nicht.'
    'error|CreateOrder|Create_web_order|Typ von body/priority ändert sich von integer zu string; der Flow setzt das Feld.'
    "error|CreateOrder|Create_web_order|Wert 'Mail' von body/channel ist nicht mehr erlaubt (erlaubt: Web, Phone)."
    'info|GetLegacyReport||Operation GetLegacyReport ist deprecated; kein Flow nutzt sie mehr.'
    'info|ListCustomers||Operation ListCustomers entfällt; kein Flow nutzt sie.'
    'warning|ArchiveOrder|Archive_order|Operation ArchiveOrder ist deprecated und wird noch genutzt.'
    'warning|CancelOrder|Cancel_old_order|Operation CancelOrder entfällt, wird aber noch genutzt.'
    'warning|CreateOrder|Create_other_order|Pflichtfeld body/costCenter ist neu; der Flow setzt es per Ausdruck, ob es dabei ist, ist nicht prüfbar.'
    'warning|CreateOrder|Create_other_order|Typ von body/priority ändert sich von integer zu string; der Flow setzt es per Ausdruck, nicht prüfbar.'
    'warning|CreateOrder|Create_other_order|Werte Mail von body/channel entfallen; der Flow setzt das Feld per Ausdruck, nicht prüfbar.'
    'warning|CreateOrder|Create_phone_order|Typ von body/priority ändert sich von integer zu string; der Flow setzt das Feld.'
    'warning|CreateOrder|Create_phone_order|Werte Mail von body/channel entfallen; der Flow setzt das Feld per Ausdruck, nicht prüfbar.'
    'warning|CreateOrder|Create_web_order|Feld body/note entfällt; der Flow setzt es, der Wert kommt nicht mehr an.'
    'warning|GetOrder|Get_order|Antwortfeld customer/email entfällt; der Flow liest customer/email.'
    'warning|GetOrder|Get_order|Antwortfeld history entfällt; der Flow liest history.'
    'warning|ListOrders|List_orders|Antwortfeld value/customer/email entfällt; der Flow liest value/customer/email.'
) | Sort-Object
Test-Case 'Breaking Changes: Fehler für aktive Flows, Warnungen für Entwürfe und Unscharfes' {
    $result = Compare-SeedConnectorOperations $old $new $usages
    Assert-Equal $expected (Get-Lines $result.Findings) 'Befunde'
    Assert-Equal ([ordered]@{
            CancelOrder = 'entfernt'; ListCustomers = 'entfernt'; ListOrders = 'geändert'; CreateOrder = 'geändert'; GetOrder = 'geändert'
            ArchiveOrder = 'deprecated'; GetLegacyReport = 'deprecated'; GetHealth = 'unverändert'
        }) $result.States 'Stand je Operation'
    $flow = @($result.Findings | Where-Object { $_.ActionPath -like '*/Cancel_order' })[0]
    Assert-Equal @('Bestellungen abgleichen', 'aktiv', 'Team Vertrieb') @($flow.Flow, $flow.State, $flow.Owner) 'Flow im Befund'
}
Test-Case 'allowBreakingConnectorChanges: alle Fehler werden Warnungen' {
    $result = Compare-SeedConnectorOperations $old $new $usages
    Set-SeedOverride $result.Findings
    Assert-Equal 0 @($result.Findings | Where-Object Severity -eq 'error').Count 'Fehler'
    Assert-Equal 15 @($result.Findings | Where-Object Severity -eq 'warning').Count 'Warnungen'
    Assert-Equal 4 @($result.Findings | Where-Object Message -like '*(übersteuert mit allowBreakingConnectorChanges)').Count 'markiert'
}
Test-Case 'Nutzung nicht prüfbar: Warnung statt Entwarnung' {
    $result = Compare-SeedConnectorOperations $old $new @() $false
    Assert-Equal @(
        'info|ArchiveOrder||Operation ArchiveOrder ist deprecated; ob Flows sie nutzen, ist nicht prüfbar.'
        'info|GetLegacyReport||Operation GetLegacyReport ist deprecated; ob Flows sie nutzen, ist nicht prüfbar.'
        'warning|CancelOrder||Operation CancelOrder entfällt; ob Flows sie nutzen, ist nicht prüfbar.'
        'warning|ListCustomers||Operation ListCustomers entfällt; ob Flows sie nutzen, ist nicht prüfbar.'
    ) (Get-Lines $result.Findings) 'Befunde'
}
Test-Case 'Übergang Bfs.Seed.OpenApi 0.9.0 zu 0.10.0: Query und Pfad nur Warnungen, Body unverändert streng' {
    $o = Get-SeedConnectorOperations (Read-Document 'openapi-enum-090.json')
    $n = Get-SeedConnectorOperations (Read-Document 'openapi-enum-010.json')
    $flow = New-Flow 'flow-enum.json' 'Tickets abgleichen' 1
    $result = Compare-SeedConnectorOperations $o $n @(Get-SeedFlowUsages $flow 'shared_example-5fticketsdev-5f0000000000000000')
    $hint = 'Seed-Functions lesen Query- und Pfadparameter als Text und nehmen die Zahl weiter an; den Flow auf die Namen umstellen.'
    Assert-Equal (@(
            "error|CreateTicket|Create_urgent_ticket|Wert '7' von body/priority ist nicht mehr erlaubt (erlaubt: 0, 1, 2)."
            "warning|CountTickets|Count_open|Typ von state ändert sich von integer zu string mit den Werten Open, Closed; der Flow setzt '0'. $hint"
            "warning|CountTickets|Count_tickets|Typ von state ändert sich von integer zu string mit den Werten Open, Closed; der Flow setzt das Feld. $hint"
            "warning|ListTickets|List_tickets|Typ von kinds ändert sich von array zu string mit den Werten Bug, Question; der Flow setzt '1'. $hint"
            "warning|ListTickets|List_tickets|Typ von levels[] ändert sich von integer zu string mit den Werten Low, Normal, High; der Flow setzt '0', '2'. $hint"
            "warning|ListTickets|List_tickets|Typ von status ändert sich von integer zu string mit den Werten Open, Closed; der Flow setzt '1'. $hint"
        ) | Sort-Object) (Get-Lines $result.Findings) 'Befunde'
    Assert-Equal ([ordered]@{ ListTickets = 'geändert'; CreateTicket = 'geändert'; CountTickets = 'geändert' }) $result.States 'Stand'
}
Test-Case 'Typwechsel object zu string, der Flow setzt Felder darin: Fehler, kein „entfällt“' {
    $address = @{ type = 'object'; properties = @{ street = @{ type = 'string' }; city = @{ type = 'string' } } }
    $o = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ post = @{ operationId = 'PostX'; parameters = @(@{ name = 'body'; in = 'body'; schema = @{ type = 'object'; properties = @{ address = $address } } }) } } } }
    $n = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ post = @{ operationId = 'PostX'; parameters = @(@{ name = 'body'; in = 'body'; schema = @{ type = 'object'; properties = @{ address = @{ type = 'string' } } } }) } } } }
    $usage = [pscustomobject]@{ OperationId = 'PostX'; StateCode = 1; Parameters = Get-SeedSetFields @{ 'body/address/street' = 'Weg 1' }; OutputPaths = @(); ActionPath = '/X' }
    Assert-Equal @('error|PostX|X|Typ von body/address ändert sich von object zu string; der Flow setzt Felder darin.') (Get-Lines (Compare-SeedConnectorOperations $o $n @($usage)).Findings) 'Befunde'
}
Test-Case 'operationIds unterscheiden Groß- und Kleinschreibung' {
    $o = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ get = @{ operationId = 'getOrder' } } } }
    $n = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ get = @{ operationId = 'GetOrder' } } } }
    $usage = [pscustomobject]@{ OperationId = 'getOrder'; StateCode = 1; Parameters = Get-SeedSetFields @{}; OutputPaths = @(); ActionPath = '/X' }
    $result = Compare-SeedConnectorOperations $o $n @($usage)
    Assert-Equal @('error|getOrder|X|Operation getOrder entfällt, wird aber noch genutzt.') (Get-Lines $result.Findings) 'Befunde'
    Assert-Equal @('getOrder=entfernt', 'GetOrder=neu') @($result.States.PSBase.Keys | ForEach-Object { "$_=$($result.States[$_])" }) 'Stand'
}
Test-Case 'Neue Werteliste an einem Feld, das bisher frei war' {
    $o = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ get = @{ operationId = 'GetX'; parameters = @(@{ name = 'mode'; in = 'query'; type = 'string' }) } } } }
    $n = Get-SeedConnectorOperations @{ paths = @{ '/x' = @{ get = @{ operationId = 'GetX'; parameters = @(@{ name = 'mode'; in = 'query'; type = 'string'; enum = @('fast', 'slow') }) } } } }
    $usage = [pscustomobject]@{ OperationId = 'GetX'; StateCode = 1; Parameters = Get-SeedSetFields @{ mode = 'Fast' }; OutputPaths = @(); ActionPath = '/X' }
    Assert-Equal @("error|GetX|X|Wert 'Fast' von mode ist nicht mehr erlaubt (erlaubt: fast, slow).") (Get-Lines (Compare-SeedConnectorOperations $o $n @($usage)).Findings) 'Befunde'
}

Write-Host 'Verbindungen'
Test-Case 'Verbindungen ohne App-Rolle, mit Fehlerstatus, ungenutzt und unbekannt' {
    $rows = @(
        [pscustomobject]@{ Connection = 'c1'; Account = 'a@example.org'; Status = 'Connected'; Used = $true; Roles = @('Bestellung') }
        [pscustomobject]@{ Connection = 'c2'; Account = 'b@example.org'; Status = 'Connected'; Used = $true; Roles = @('Standardzugriff') }
        [pscustomobject]@{ Connection = 'c3'; Account = 'c@example.org'; Status = 'Error'; Used = $false; Roles = @() }
        [pscustomobject]@{ Connection = 'c4'; Account = 'd@example.org'; Status = 'Connected'; Used = $null; Roles = @('?'); RoleError = '403 (Forbidden) Insufficient privileges.' }
        [pscustomobject]@{ Connection = 'c5'; Account = $null; Status = 'Connected'; Used = $null; Roles = @() }
        [pscustomobject]@{ Connection = 'c6'; Account = 'e@example.org'; Status = 'Connected'; Used = $true; Roles = @('?'); RoleError = '403 (Forbidden) Insufficient privileges.' }
    )
    Assert-Equal @(
        'info|||Verbindung c3 (c@example.org) hat den Status Error. Weder ein Flow noch eine Connection Reference nutzt sie.'
        'warning|||Verbindung c2 (b@example.org): Das Konto hat keine App-Rolle an Bestellungen API; Aufrufe über diese Verbindung scheitern.'
        'warning|||Verbindung c5 (Konto unbekannt): Das Konto hat keine App-Rolle an Bestellungen API; Aufrufe über diese Verbindung scheitern.'
        'warning|||Verbindungen nicht prüfbar: Für 2 Verbindungen sind die App-Rollen der Konten nicht ermittelbar (403 (Forbidden) Insufficient privileges.). Die Pipeline-Identität braucht dafür Graph-Rechte (README, Prüfung des Custom Connectors).'
    ) (Get-Lines @(Get-SeedConnectionFindings $rows 'Bestellungen API')) 'Befunde'
}
Test-Case 'Genutzt: direkt im Flow eingebunden oder über eine Connection Reference' {
    Assert-Equal $true (Get-SeedConnectionUse 'c1' $null @('c1')) 'Flow außerhalb einer Solution, Connection References nicht lesbar'
    Assert-Equal $true (Get-SeedConnectionUse 'c2' @('c2') @()) 'Connection Reference'
    Assert-Equal $false (Get-SeedConnectionUse 'c3' @('c2') @('c1')) 'ungenutzt'
    Assert-Equal $null (Get-SeedConnectionUse 'c3' $null @('c1')) 'unbekannt'
}

Write-Host 'Wiederholen'
function New-HttpError([int] $Status, [int] $RetryAfter = 0) {
    $response = [Net.Http.HttpResponseMessage]::new([Net.HttpStatusCode] $Status)
    if ($RetryAfter) { $response.Headers.RetryAfter = [Net.Http.Headers.RetryConditionHeaderValue]::new([TimeSpan]::FromSeconds($RetryAfter)) }
    [Microsoft.PowerShell.Commands.HttpResponseException]::new("Status $Status", $response)
}
Test-Case '429 mit Retry-After, 503 ohne, andere Fehler sofort' {
    $calls = @{ n = 0 }
    $waits = [Collections.Generic.List[int]]::new()
    $errors = @((New-HttpError 429 2), (New-HttpError 503))
    $result = Invoke-SeedRetry { if ($calls.n -lt 2) { $e = $errors[$calls.n]; $calls.n++; throw $e }; 'ok' } -Wait { param($s) $waits.Add($s) }
    Assert-Equal 'ok' $result 'Ergebnis'
    Assert-Equal @(2, 5) @($waits) 'Wartezeiten'
    $calls.n = 0
    try { Invoke-SeedRetry { $calls.n++; throw (New-HttpError 404) } -Wait { param($s) $waits.Add($s) }; throw 'kein Fehler' }
    catch { if ($_.Exception.Message -ne 'Status 404') { throw } }
    Assert-Equal 1 $calls.n '404 ohne Wiederholung'
    $calls.n = 0
    try { Invoke-SeedRetry { $calls.n++; throw (New-HttpError 503) } -Wait { param($s) } ; throw 'kein Fehler' }
    catch { if ($_.Exception.Message -ne 'Status 503') { throw } }
    Assert-Equal 3 $calls.n 'höchstens zwei Wiederholungen'
}

Write-Host 'Ausgabe'
Test-Case 'Log ohne Logging-Befehle und Zeilenumbrüche' {
    Assert-Equal 'Flow a ##vso_[task.setvariable variable=x]1' (Format-SeedLogText "Flow a`n##vso[task.setvariable variable=x]1") 'bereinigt'
    Assert-Equal 'x ##vso_[task.complete]' (Format-SeedLogText "x ##VSO[task.complete]") 'Großschreibung'
}
Test-Case 'Markdown-Zellen ohne Links, Spalten und HTML' {
    Assert-Equal 'A\|B \[x\](https://example.org) &lt;b&gt; \_y\_' (Format-SeedMarkdown 'A|B [x](https://example.org) <b> _y_') 'entschärft'
}
Test-Case 'Link nur mit resourceid' {
    Assert-Equal 'https://make.powerautomate.com/environments/Default-00000000-0000-0000-0000-000000000000/flows/11111111-2222-3333-4444-555555555555/details' `
    (Get-SeedFlowLink 'Default-00000000-0000-0000-0000-000000000000' '11111111-2222-3333-4444-555555555555') 'Link'
    Assert-Equal $null (Get-SeedFlowLink 'Default-00000000-0000-0000-0000-000000000000' '') 'Entwurf'
}
Test-Case 'Zusammenfassung' {
    $result = Compare-SeedConnectorOperations $old $new $usages
    $md = New-SeedConnectorSummary -DisplayName 'Bestellungen | dev' -InternalId $internalId -EnvironmentUrl 'https://org0000000.crm4.dynamics.com' `
        -EnvironmentId 'Default-00000000-0000-0000-0000-000000000000' -Backend 'https://api.example.org/api' -States $result.States `
        -OldOperations $old -NewOperations $new -Usages $usages -Findings $result.Findings -FlowCheck '2 Flows' -ConnectionCheck 'aus'
    $text = $md -join "`n"
    foreach ($part in '# Custom Connector: Bestellungen \| dev', '**4 Fehler, 11 Warnungen**', '## Operationen', '| CancelOrder | entfernt | 2 | POST /orders/{id}/cancel |',
        '## Befunde', '## Flows', '[Bestellungen abgleichen](https://make.powerautomate.com/environments/Default-00000000-0000-0000-0000-000000000000/flows/11111111-2222-3333-4444-555555555555/details)',
        '| Alte Bestellungen | aus | Team Vertrieb | CancelOrder, CreateOrder |') {
        if (-not $text.Contains($part)) { throw "Fehlt: $part" }
    }
    if ($text.IndexOf('| Fehler |') -gt $text.IndexOf('| Warnung |')) { throw 'Fehler stehen nicht vor Warnungen.' }
}

Write-Host 'Skript'
# Ohne Netzwerk: Eine ungültige Umgebungs-URL scheitert vor jedem Aufruf. Prüft Exit-Code, Log und
# das Übersteuern.
Test-Case 'Fehler endet mit Exit 1, übersteuert mit Exit 0' {
    $script = Join-Path $PSScriptRoot 'Test-SeedConnectorChange.ps1'
    $summary = Join-Path ([IO.Path]::GetTempPath()) "seed-connector-test-$([guid]::NewGuid()).md"
    $out = pwsh -NoProfile -File $script -EnvironmentUrl 'http://example.org' -Name 'ordersdev' -OpenApiFile 'missing.json' -SummaryFile $summary
    Assert-Equal 1 $LASTEXITCODE 'Exit-Code'
    if (-not ($out -match '^##vso\[task\.logissue type=error\]Prüfung des Custom Connectors gescheitert: Ungültige Umgebungs-URL')) { throw "Log: $out" }
    $out = pwsh -NoProfile -File $script -EnvironmentUrl 'http://example.org' -Name 'ordersdev' -OpenApiFile 'missing.json' -SummaryFile $summary -AllowBreakingChanges
    Assert-Equal 0 $LASTEXITCODE 'Exit-Code übersteuert'
    if (-not ($out -match '^##vso\[task\.logissue type=warning\].*übersteuert')) { throw "Log: $out" }
    if (-not (Get-Content -Raw $summary).Contains('**0 Fehler, 1 Warnungen**')) { throw 'Zusammenfassung fehlt.' }
    Remove-Item $summary
}

Write-Host ''
if ($failures.Count) {
    Write-Host "$($failures.Count) von $count Tests gescheitert." -ForegroundColor Red
    exit 1
}
Write-Host "Alle $count Tests bestanden." -ForegroundColor Green
