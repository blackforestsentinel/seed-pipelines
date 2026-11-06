#Requires -Version 7.2
# Vergleichslogik der Connector-Prüfung (Test-SeedConnectorChange.ps1), ohne Netzwerk, damit sie mit
# Testdaten prüfbar ist (SeedConnectorCheck.Tests.ps1). Daten aus OpenAPI und clientdata kommen als
# Hashtables aus ConvertFrom-Json -AsHashtable; Felder werden per Index gelesen, nicht per Punkt:
# Ein Feld namens keys oder count verdeckte sonst die Eigenschaften der Hashtable.

$script:Methods = 'get', 'put', 'post', 'delete', 'patch', 'head', 'options'

# Antwortfelder im Ausdruck: body('Action')?['a']?['b'] und outputs('Action')?['body/a/b']
$script:OutputPattern = [regex] "(?<fn>body|outputs)\(\s*'(?<action>[^']+)'\s*\)(?<path>(?:\??\[\s*'[^']*'\s*\])*)"
# Element einer Schleife: items('Schleife')?['a']
$script:ItemsPattern = [regex] "items\(\s*'(?<loop>[^']+)'\s*\)(?<path>(?:\??\[\s*'[^']*'\s*\])*)"
$script:SegmentPattern = [regex] "\[\s*'([^']*)'\s*\]"

function Get-SeedKeys($Map) {
    if ($Map -is [Collections.IDictionary]) { @($Map.PSBase.Keys) } else { @() }
}

# --- Ausgabe -------------------------------------------------------------------------------------

# Meldungen enthalten Daten aus Dataverse und Power Platform, die jede Person mit Maker-Rechten
# setzen kann: ohne Steuerzeichen und ##vso[ ins Log (wie Fail in custom-connector.yml), sonst
# könnten sie Logging-Befehle ausgeben.
function Format-SeedLogText([string] $Text) {
    ($Text -replace '[\x00-\x1f\x7f]', ' ') -replace '##vso\[', '##vso_['
}

# Für Tabellenzellen der Zusammenfassung: dazu HTML und Markdown-Zeichen entschärfen, damit ein
# Flow-Name keine Links, Tabellenspalten oder Formatierung einschleust.
function Format-SeedMarkdown([string] $Text) {
    $text = (Format-SeedLogText $Text).Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
    $text -replace '([\\`*_\[\]|])', '\$1'
}

function Get-SeedFlowState([int] $StateCode) {
    switch ($StateCode) {
        0 { 'aus' }
        1 { 'aktiv' }
        2 { 'angehalten' }
        default { "Status $StateCode" }
    }
}

# Breaking Changes stoppen den Lauf nur für Flows, die laufen oder angehalten sind (statecode 1
# Activated, 2 Suspended). Ausgeschaltete Flows und Entwürfe (0 Draft) bekommen eine Warnung.
function Get-SeedBreakingSeverity($Usage) {
    if ($Usage.StateCode -in 1, 2) { 'error' } else { 'warning' }
}

function New-SeedFinding([string] $Severity, [string] $Operation, [string] $Message, $Usage = $null) {
    [pscustomobject]@{
        Severity   = $Severity
        Operation  = $Operation
        Message    = $Message
        Flow       = $Usage.Flow
        FlowId     = $Usage.FlowId
        ResourceId = $Usage.ResourceId
        State      = $Usage.State
        Owner      = $Usage.Owner
        ActionPath = $Usage.ActionPath
    }
}

# allowBreakingConnectorChanges: alle Fehler werden zu Warnungen, wie confirmDataDeletion.
function Set-SeedOverride($Findings) {
    foreach ($f in $Findings) {
        if ($f.Severity -eq 'error') {
            $f.Severity = 'warning'
            $f.Message += ' (übersteuert mit allowBreakingConnectorChanges)'
        }
    }
}

# --- OpenAPI: Operationen mit Ein- und Antwortfeldern -----------------------------------------------
# Eingabefelder heißen wie in inputs.parameters der Flows: Query-, Pfad- und Header-Parameter mit
# ihrem Namen, Body-Felder mit dem Namen des Body-Parameters davor (body/customer, body/lines[]/sku).
# Antwortfelder ohne Präfix (customer/email), Listen mit [].

function Resolve-SeedRef($Node, $Document) {
    for ($i = 0; $Node -is [Collections.IDictionary] -and $Node['$ref'] -is [string] -and $i -lt 20; $i++) {
        if ($Node['$ref'] -notmatch '^#/(?<section>definitions|parameters|responses)/(?<name>.+)$') { return $null }
        $section = $Document[$Matches.section]
        $Node = if ($section -is [Collections.IDictionary]) { $section[$Matches.name] } else { $null }
    }
    $Node
}

# allOf (Vererbung) zu einem Schema mit allen Eigenschaften zusammenführen
function Merge-SeedAllOf($Schema, $Document, [int] $Depth = 0) {
    if ($Schema -isnot [Collections.IDictionary] -or $Schema['allOf'] -isnot [Collections.IList] -or $Depth -gt 8) { return $Schema }
    $merged = [ordered]@{}
    foreach ($key in Get-SeedKeys $Schema) { if ($key -ne 'allOf') { $merged[$key] = $Schema[$key] } }
    $properties = [ordered]@{}
    $required = [Collections.Generic.List[string]]::new()
    $parts = @(@($Schema['allOf']) | ForEach-Object { Merge-SeedAllOf (Resolve-SeedRef $_ $Document) $Document ($Depth + 1) }) + @($merged)
    foreach ($part in $parts) {
        if ($part -isnot [Collections.IDictionary]) { continue }
        foreach ($name in Get-SeedKeys $part['properties']) { $properties[$name] = $part['properties'][$name] }
        foreach ($name in @($part['required'])) { if ($name) { $required.Add([string] $name) } }
        if (-not $merged['type'] -and $part['type']) { $merged['type'] = $part['type'] }
    }
    $merged['properties'] = $properties
    $merged['required'] = @($required)
    $merged
}

function Get-SeedTypeName($Schema) {
    $type = @($Schema['type']) | Where-Object { $_ -and $_ -ne 'null' } | Select-Object -First 1
    if (-not $type -and $Schema['properties'] -is [Collections.IDictionary]) { $type = 'object' }
    [string] $type
}

# Wörterbuch mit Reihenfolge, das Schlüssel genau vergleicht: operationIds unterscheiden Groß- und
# Kleinschreibung (getOrder zu GetOrder ist für Flows eine entfernte und eine neue Operation).
function New-SeedOrdinalMap {
    [Collections.Specialized.OrderedDictionary]::new([StringComparer]::Ordinal)
}

# Required gilt innerhalb des übergeordneten Objekts (Parent); ob das Objekt selbst mitkommt,
# entscheidet Test-SeedPresent. In ist der Ort des Parameters: body, query, path, header.
function Add-SeedField($Into, $Schema, $Document, [string] $Path, [string] $Parent, [bool] $Required, [int] $Depth, [string] $In = 'body') {
    $Schema = Merge-SeedAllOf (Resolve-SeedRef $Schema $Document) $Document
    if ($Schema -isnot [Collections.IDictionary] -or $Depth -gt 8) { return }
    $type = Get-SeedTypeName $Schema
    if ($Path) {
        $Into[$Path] = [pscustomobject]@{
            Type       = $type
            Format     = [string] $Schema['format']
            Enum       = @(if ($Schema['enum'] -is [Collections.IList]) { $Schema['enum'] | ForEach-Object { [string] $_ } })
            Required   = $Required
            Parent     = $Parent
            HasDefault = $Schema.Contains('default')
            In         = $In
        }
    }
    if ($Schema['properties'] -is [Collections.IDictionary]) {
        $requiredNames = @($Schema['required'])
        foreach ($name in Get-SeedKeys $Schema['properties']) {
            $child = if ($Path) { "$Path/$name" } else { $name }
            Add-SeedField $Into $Schema['properties'][$name] $Document $child $Path ($requiredNames -contains $name) ($Depth + 1) $In
        }
    }
    elseif ($type -eq 'array') {
        Add-SeedField $Into $Schema['items'] $Document "$($Path)[]" $Path $false ($Depth + 1) $In
    }
}

function Get-SeedConnectorOperations($Document) {
    $operations = New-SeedOrdinalMap
    if ($Document -isnot [Collections.IDictionary]) { return $operations }
    foreach ($path in Get-SeedKeys $Document['paths']) {
        $item = $Document['paths'][$path]
        if ($item -isnot [Collections.IDictionary]) { continue }
        $shared = @(@($item['parameters']) | ForEach-Object { Resolve-SeedRef $_ $Document } | Where-Object { $_ -is [Collections.IDictionary] })
        foreach ($method in Get-SeedKeys $item) {
            $op = $item[$method]
            if ($method -notin $script:Methods -or $op -isnot [Collections.IDictionary] -or -not $op['operationId']) { continue }
            $own = @(@($op['parameters']) | ForEach-Object { Resolve-SeedRef $_ $Document } | Where-Object { $_ -is [Collections.IDictionary] })
            # Parameter der Operation ersetzen gleichnamige des Pfads
            $parameters = @($shared | Where-Object { $s = $_; -not ($own | Where-Object { $_['name'] -eq $s['name'] -and $_['in'] -eq $s['in'] }) }) + $own

            $inputs = [ordered]@{}
            foreach ($p in $parameters) {
                if ($p['in'] -eq 'body') {
                    $name = if ($p['name']) { [string] $p['name'] } else { 'body' }
                    Add-SeedField $inputs $p['schema'] $Document $name '' ([bool] $p['required']) 0
                }
                elseif ($p['name']) {
                    # Query-, Pfad- und Header-Parameter tragen type, enum und items wie ein Schema.
                    Add-SeedField $inputs $p $Document ([string] $p['name']) '' ([bool] $p['required']) 0 ([string] $p['in'])
                }
            }

            $outputs = [ordered]@{}
            $responses = $op['responses']
            foreach ($code in @(Get-SeedKeys $responses | Where-Object { $_ -match '^2\d\d$' } | Sort-Object)) {
                $response = Resolve-SeedRef $responses[$code] $Document
                if ($response -is [Collections.IDictionary] -and $response['schema']) {
                    Add-SeedField $outputs $response['schema'] $Document '' '' $false 0
                    break
                }
            }

            $operations[[string] $op['operationId']] = [pscustomobject]@{
                Method     = $method.ToUpperInvariant()
                Path       = $path
                Deprecated = $op['deprecated'] -eq $true
                Inputs     = $inputs
                Outputs    = $outputs
            }
        }
    }
    $operations
}

# Vergleichbare Form ohne deprecated: Hat sich an der Operation sonst etwas geändert?
function Get-SeedSignature($Operation) {
    [ordered]@{ Method = $Operation.Method; Path = $Operation.Path; Inputs = $Operation.Inputs; Outputs = $Operation.Outputs } |
        ConvertTo-Json -Depth 6 -Compress
}

# --- Flows: welche Action ruft welche Operation des Connectors ---------------------------------------

# Alle Actions und Trigger rekursiv: actions, triggers, else, default, cases; Scopes und Schleifen
# haben selbst actions. Path ist der Weg durch die Definition, etwa /Try/For_each/Check/else/Buchen.
function Get-SeedFlowActions($Node, [string] $Path = '', [int] $Depth = 0) {
    if ($Node -isnot [Collections.IDictionary] -or $Depth -gt 64) { return }
    foreach ($key in 'triggers', 'actions') {
        if ($Node[$key] -isnot [Collections.IDictionary]) { continue }
        foreach ($name in Get-SeedKeys $Node[$key]) {
            [pscustomobject]@{ Name = [string] $name; Path = "$Path/$name"; Action = $Node[$key][$name] }
            Get-SeedFlowActions $Node[$key][$name] "$Path/$name" ($Depth + 1)
        }
    }
    if ($Node['else'] -is [Collections.IDictionary]) { Get-SeedFlowActions $Node['else'] "$Path/else" ($Depth + 1) }
    foreach ($case in Get-SeedKeys $Node['cases']) {
        Get-SeedFlowActions $Node['cases'][$case] "$Path/$case" ($Depth + 1)
    }
    if ($Node['default'] -is [Collections.IDictionary]) { Get-SeedFlowActions $Node['default'] "$Path/default" ($Depth + 1) }
}

# Alle Texte der Definition, außer der Liste einer Schleife (foreach): Was die Schleife davon liest,
# zeigen ihre items(); die Liste selbst als gelesen zu zählen, meldete jedes entfallene Unterfeld.
function Get-SeedStrings($Node, $Into, [int] $Depth = 0) {
    if ($Depth -gt 64) { return }
    if ($Node -is [string]) { $Into.Add($Node) }
    elseif ($Node -is [Collections.IDictionary]) {
        foreach ($key in Get-SeedKeys $Node) {
            if ($key -eq 'foreach' -and $Node[$key] -is [string]) { continue }
            Get-SeedStrings $Node[$key] $Into ($Depth + 1)
        }
    }
    elseif ($Node -is [Collections.IList]) { foreach ($value in $Node) { Get-SeedStrings $value $Into ($Depth + 1) } }
}

function Get-SeedSegments([string] $Path) {
    @($script:SegmentPattern.Matches($Path) | ForEach-Object { $_.Groups[1].Value -split '/' } | Where-Object { $_ -ne '' })
}

# body('A')?['x'] bzw. outputs('A')?['body/x'] als Action A mit Pfad x; outputs ohne body (etwa
# headers, statusCode) liest keine Antwortfelder.
function ConvertTo-SeedOutputReference($Match) {
    $segments = Get-SeedSegments $Match.Groups['path'].Value
    if ($Match.Groups['fn'].Value -eq 'outputs') {
        if ($segments.Count -eq 0 -or $segments[0] -ne 'body') { return $null }
        $segments = @($segments | Select-Object -Skip 1)
    }
    [pscustomobject]@{ Action = $Match.Groups['action'].Value; Segments = $segments }
}

# Quelle einer Schleife: foreach über body('A')?['liste'] oder über items('Äußere')?['liste']
function Resolve-SeedLoopSource([string] $Loop, $Loops, [int] $Depth = 0) {
    $expression = $Loops[$Loop]
    if (-not $expression -or $Depth -gt 8) { return $null }
    $match = $script:OutputPattern.Match($expression)
    if ($match.Success) { return ConvertTo-SeedOutputReference $match }
    $match = $script:ItemsPattern.Match($expression)
    if ($match.Success) {
        $outer = Resolve-SeedLoopSource $match.Groups['loop'].Value $Loops ($Depth + 1)
        if ($outer) {
            return [pscustomobject]@{ Action = $outer.Action; Segments = @($outer.Segments) + @(Get-SeedSegments $match.Groups['path'].Value) }
        }
    }
    $null
}

# Welche Antwortfelder liest der Flow per Ausdruck, je Action? Unscharf: Erkannt werden body(),
# outputs() und items() von Schleifen über eine Antwort; Zugriffe über Variablen, item() in Select
# oder Filter und Actions, die die ganze Antwort weiterreichen, sieht diese Suche nicht.
function Get-SeedOutputPaths($Definition) {
    $paths = @{}
    $strings = [Collections.Generic.List[string]]::new()
    Get-SeedStrings $Definition $strings
    $loops = @{}
    foreach ($a in Get-SeedFlowActions $Definition) {
        if ($a.Action -is [Collections.IDictionary] -and $a.Action['type'] -eq 'Foreach' -and $a.Action['foreach'] -is [string]) {
            $loops[$a.Name] = $a.Action['foreach']
        }
    }
    $add = {
        param($Action, $Segments)
        if (@($Segments).Count -eq 0) { return }
        if (-not $paths.ContainsKey($Action)) { $paths[$Action] = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase) }
        [void] $paths[$Action].Add((@($Segments) -join '/'))
    }
    foreach ($text in $strings) {
        foreach ($m in $script:OutputPattern.Matches($text)) {
            $reference = ConvertTo-SeedOutputReference $m
            if ($reference) { & $add $reference.Action $reference.Segments }
        }
        foreach ($m in $script:ItemsPattern.Matches($text)) {
            $source = Resolve-SeedLoopSource $m.Groups['loop'].Value $loops
            if ($source) { & $add $source.Action (@($source.Segments) + @(Get-SeedSegments $m.Groups['path'].Value)) }
        }
    }
    $paths
}

# inputs.parameters einer Action als flache Liste: body/customer, body/lines[]/sku. Ein Objekt oder
# eine Liste als Wert (Body als JSON eingegeben) wird aufgefaltet; der Wert selbst bleibt auch drin.
function Get-SeedSetFields($Parameters) {
    $fields = [ordered]@{}
    foreach ($key in Get-SeedKeys $Parameters) { Add-SeedSetField $fields ([string] $key) $Parameters[$key] 0 }
    $fields
}

function Add-SeedSetField($Fields, [string] $Path, $Value, [int] $Depth) {
    $Fields[$Path] = $Value
    if ($Depth -gt 16) { return }
    if ($Value -is [Collections.IDictionary]) {
        foreach ($key in Get-SeedKeys $Value) { Add-SeedSetField $Fields "$Path/$key" $Value[$key] ($Depth + 1) }
    }
    elseif ($Value -is [Collections.IList]) {
        $items = @($Value)
        if ($items | Where-Object { $_ -is [Collections.IDictionary] }) {
            foreach ($item in $items) {
                foreach ($key in Get-SeedKeys $item) { Add-SeedSetField $Fields "$($Path)[]/$key" $item[$key] ($Depth + 1) }
            }
        }
        else { $Fields["$($Path)[]"] = $items }
    }
}

function Test-SeedExpression($Value) {
    $Value -is [string] -and ($Value.StartsWith('@') -or $Value.Contains('@{'))
}

# set: der Flow setzt das Feld; unknown: ein übergeordnetes Feld kommt als Ganzes, etwa per Ausdruck,
# ob das Feld darin steckt, ist nicht prüfbar; unset: der Flow setzt es nicht.
function Get-SeedFieldState($Fields, [string] $Field) {
    if ($Fields.Contains($Field)) { return 'set' }
    $segments = $Field -split '/'
    $prefix = ''
    for ($i = 0; $i -lt $segments.Count - 1; $i++) {
        $prefix = if ($prefix) { "$prefix/$($segments[$i])" } else { $segments[$i] }
        foreach ($candidate in @($prefix, ($prefix -replace '\[\]$', '')) | Select-Object -Unique) {
            if (-not $Fields.Contains($candidate)) { continue }
            $value = $Fields[$candidate]
            $hasObjects = $value -is [Collections.IList] -and (@($value) | Where-Object { $_ -is [Collections.IDictionary] })
            if ($value -isnot [Collections.IDictionary] -and -not $hasObjects) { return 'unknown' }
        }
    }
    'unset'
}

# Kommt das Objekt Path im Aufruf mit? Ja, wenn der Flow es oder etwas darin setzt oder wenn es
# Pflicht ist und sein übergeordnetes Objekt mitkommt.
function Test-SeedPresent($Fields, $Spec, [string] $Path, [int] $Depth = 0) {
    if (-not $Path) { return $true }
    if ($Depth -gt 16) { return $false }
    $plain = $Path -replace '\[\]$', ''
    foreach ($key in Get-SeedKeys $Fields) {
        if ($key -eq $Path -or $key -eq $plain -or $key.StartsWith("$Path/") -or $key.StartsWith("$($plain)[]") -or $key.StartsWith("$plain/")) { return $true }
    }
    $node = $Spec[$Path]
    [bool] ($node -and $node.Required -and (Test-SeedPresent $Fields $Spec $node.Parent ($Depth + 1)))
}

# Connector eines Eintrags in properties.connectionReferences: api.name ist die connectorinternalid,
# Flows außerhalb von Solutions nennen ihn auch nur in id (/providers/.../apis/<connectorinternalid>).
function Get-SeedReferenceApi($Reference) {
    if ($Reference -isnot [Collections.IDictionary]) { return '' }
    $api = if ($Reference['api'] -is [Collections.IDictionary]) { $Reference['api']['name'] }
    if (-not $api -and $Reference['id']) { $api = ([string] $Reference['id'] -split '/')[-1] }
    [string] $api
}

function Get-SeedClientData($Flow) {
    $data = $Flow.ClientData | ConvertFrom-Json -AsHashtable
    if ($data -isnot [Collections.IDictionary] -or $data['properties'] -isnot [Collections.IDictionary]) { throw 'clientdata ohne properties.' }
    $data
}

# Verweise eines Flows auf den Connector: Schlüssel in connectionReferences und die Verbindungen, die
# er direkt einbindet. Flows in Solutions gehen über eine Connection Reference
# (connection.connectionReferenceLogicalName); Flows außerhalb nennen die Verbindung selbst
# (connectionName oder connection.name) und legen keine Connection Reference an.
function Get-SeedFlowReferences($Data, [string] $InternalId) {
    $keys = [Collections.Generic.List[string]]::new()
    $connections = [Collections.Generic.List[string]]::new()
    $references = $Data['properties']['connectionReferences']
    foreach ($key in Get-SeedKeys $references) {
        $reference = $references[$key]
        if ((Get-SeedReferenceApi $reference) -ne $InternalId) { continue }
        $keys.Add([string] $key)
        $connection = $reference['connectionName']
        if (-not $connection -and $reference['connection'] -is [Collections.IDictionary]) { $connection = $reference['connection']['name'] }
        if ($connection -is [string] -and $connection) { $connections.Add($connection) }
    }
    [pscustomobject]@{ Keys = @($keys); Connections = @($connections) }
}

# Alles, was die Prüfung aus einem Flow braucht: Actions mit Operationen des Connectors, direkt
# eingebundene Verbindungen und, falls der Flow den Connector nennt, aber keine Action erkannt wird
# (etwa eine ältere ApiConnection mit method und path), eine Warnung statt still 0 Treffer.
function Read-SeedFlow($Flow, [string] $InternalId) {
    $data = Get-SeedClientData $Flow
    $usages = @(Get-SeedFlowUsages $Flow $InternalId $data)
    $references = Get-SeedFlowReferences $data $InternalId
    $findings = @(if ($references.Keys.Count -and -not $usages.Count) {
            $flowInfo = [pscustomobject]@{ Flow = $Flow.Name; FlowId = $Flow.Id; ResourceId = $Flow.ResourceId; State = Get-SeedFlowState $Flow.StateCode; Owner = $Flow.Owner }
            New-SeedFinding warning '' 'Nutzung nicht prüfbar: Der Flow bindet den Connector ein, aber keine Action mit operationId ist erkannt (etwa eine ältere ApiConnection-Action). Bitte von Hand prüfen.' $flowInfo
        })
    [pscustomobject]@{ Usages = $usages; Connections = $references.Connections; Findings = $findings }
}

function Get-SeedFlowUsages($Flow, [string] $InternalId, $Data = $null) {
    $data = if ($null -ne $Data) { $Data } else { Get-SeedClientData $Flow }
    $properties = $data['properties']
    # connectionName einer Action -> Connector
    $references = @{}
    foreach ($key in Get-SeedKeys $properties['connectionReferences']) {
        $references[[string] $key] = Get-SeedReferenceApi $properties['connectionReferences'][$key]
    }
    $definition = $properties['definition']
    $outputPaths = Get-SeedOutputPaths $definition
    foreach ($a in Get-SeedFlowActions $definition) {
        # inputs ist nicht immer ein Objekt, bei Compose etwa ein String.
        $inputs = if ($a.Action -is [Collections.IDictionary]) { $a.Action['inputs'] }
        $target = if ($inputs -is [Collections.IDictionary]) { $inputs['host'] }
        if ($target -isnot [Collections.IDictionary] -or -not $target['operationId']) { continue }
        # Ältere Definitionen nennen connectionName, neuere connection (Name oder Objekt).
        $connection = $target['connectionName'] ?? $target['connection']
        if ($connection -is [Collections.IDictionary]) { $connection = $connection['referenceName'] ?? $connection['name'] }
        $api = if ($connection -and $references.ContainsKey([string] $connection)) { $references[[string] $connection] }
        if (-not $api -and $target['apiId']) { $api = ([string] $target['apiId'] -split '/')[-1] }
        if ($api -ne $InternalId) { continue }
        [pscustomobject]@{
            Flow        = $Flow.Name
            FlowId      = $Flow.Id
            ResourceId  = $Flow.ResourceId
            StateCode   = [int] $Flow.StateCode
            State       = Get-SeedFlowState $Flow.StateCode
            Owner       = $Flow.Owner
            Action      = $a.Name
            ActionPath  = $a.Path
            OperationId = [string] $target['operationId']
            Parameters  = Get-SeedSetFields $inputs['parameters']
            OutputPaths = if ($outputPaths.ContainsKey($a.Name)) { @($outputPaths[$a.Name]) } else { @() }
        }
    }
}

# --- Vergleich ---------------------------------------------------------------------------------------

function Format-SeedValue($Value) {
    $text = [string] $Value
    if ($text.Length -gt 40) { $text = $text.Substring(0, 40) + '…' }
    $text
}

# Setzt der Flow Felder innerhalb von Field (body/address/street für body/address)?
function Test-SeedSetsWithin($Fields, [string] $Field) {
    [bool] (Get-SeedKeys $Fields | Where-Object { $_.StartsWith("$Field/") -or $_.StartsWith("$($Field)[]") })
}

# Bfs.Seed.OpenApi ab 0.10.0 gibt Query- und Pfadparameter mit Enum-Typ als Text mit den Namen als
# enum aus, vorher als Zahl ohne Werteliste (bei Listen womöglich als Liste). Seed-Functions lesen
# diese Parameter als Text mit Enum.TryParse, das auch Zahlen annimmt: Flows mit Zahlen laufen weiter.
# Im Body gilt das nicht, dort entscheidet die JSON-Deserialisierung.
function Test-SeedEnumTransition($Old, $New) {
    [bool] ($Old -and $New -and $Old.In -ne 'body' -and $New.In -ne 'body' -and $Old.Enum.Count -eq 0 -and
        $Old.Type -in 'integer', 'number', 'array' -and $New.Type -eq 'string' -and $New.Enum.Count -gt 0)
}

# Vergleicht die bisherige Definition ($Old) mit der neuen ($New) und bewertet jede Änderung an den
# Stellen, die Flows nutzen. Ohne Leserecht auf alle Flows ($UsageKnown $false) gibt es keine
# Entwarnung „kein Flow nutzt sie“, sondern Warnungen.
function Compare-SeedConnectorOperations {
    param($Old, $New, [object[]] $Usages = @(), [bool] $UsageKnown = $true)
    $findings = [Collections.Generic.List[object]]::new()
    $add = { param($Severity, $Operation, $Message, $Usage) $findings.Add((New-SeedFinding $Severity $Operation $Message $Usage)) }
    $byOperation = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::Ordinal)
    foreach ($u in $Usages) {
        if (-not $u) { continue }
        if (-not $byOperation.ContainsKey($u.OperationId)) { $byOperation[$u.OperationId] = [Collections.Generic.List[object]]::new() }
        $byOperation[$u.OperationId].Add($u)
    }
    $states = New-SeedOrdinalMap

    foreach ($id in Get-SeedKeys $Old) {
        if ($New.Contains($id)) { continue }
        $states[$id] = 'entfernt'
        $uses = if ($byOperation.ContainsKey($id)) { @($byOperation[$id]) } else { @() }
        if (-not $UsageKnown) { & $add warning $id "Operation $id entfällt; ob Flows sie nutzen, ist nicht prüfbar." }
        elseif ($uses.Count) { foreach ($u in $uses) { & $add (Get-SeedBreakingSeverity $u) $id "Operation $id entfällt, wird aber noch genutzt." $u } }
        else { & $add info $id "Operation $id entfällt; kein Flow nutzt sie." }
    }

    foreach ($id in Get-SeedKeys $New) {
        $n = $New[$id]
        $o = $Old[$id]
        if (-not $o) { $states[$id] = 'neu'; continue }
        $uses = if ($byOperation.ContainsKey($id)) { @($byOperation[$id]) } else { @() }

        if ($n.Deprecated) {
            if (-not $UsageKnown) { & $add info $id "Operation $id ist deprecated; ob Flows sie nutzen, ist nicht prüfbar." }
            elseif ($uses.Count) { foreach ($u in $uses) { & $add warning $id "Operation $id ist deprecated und wird noch genutzt." $u } }
            else { & $add info $id "Operation $id ist deprecated; kein Flow nutzt sie mehr." }
        }

        # Felder mit gemeldetem Typwechsel: Ihre Unterfelder melden nicht noch einmal „entfällt“.
        $reportedInputs = [Collections.Generic.List[string]]::new()
        foreach ($field in Get-SeedKeys $n.Inputs) {
            $nf = $n.Inputs[$field]
            $of = $o.Inputs[$field]
            if (Test-SeedEnumTransition $of $nf) {
                $reportedInputs.Add($field)
                foreach ($u in $uses) {
                    if ((Get-SeedFieldState $u.Parameters $field) -ne 'set') { continue }
                    $numbers = @(@($u.Parameters[$field]) | Where-Object { -not (Test-SeedExpression $_) -and [string] $_ -cnotin $nf.Enum })
                    $sets = if ($numbers.Count) { "der Flow setzt $(@($numbers | ForEach-Object { "'$(Format-SeedValue $_)'" }) -join ', ')" } else { 'der Flow setzt das Feld' }
                    & $add warning $id "Typ von $field ändert sich von $($of.Type) zu string mit den Werten $($nf.Enum -join ', '); $sets. Seed-Functions lesen Query- und Pfadparameter als Text und nehmen die Zahl weiter an; den Flow auf die Namen umstellen." $u
                }
                continue
            }
            if ($nf.Required -and -not $nf.HasDefault -and -not ($of -and $of.Required)) {
                foreach ($u in $uses) {
                    if (-not (Test-SeedPresent $u.Parameters $n.Inputs $nf.Parent)) { continue }
                    switch (Get-SeedFieldState $u.Parameters $field) {
                        'unset' { & $add (Get-SeedBreakingSeverity $u) $id "Pflichtfeld $field ist neu; der Flow setzt es nicht." $u }
                        'unknown' { & $add warning $id "Pflichtfeld $field ist neu; der Flow setzt es per Ausdruck, ob es dabei ist, ist nicht prüfbar." $u }
                    }
                }
            }
            # integer zu number erweitert nur, was erlaubt ist.
            if ($of -and $of.Type -and $nf.Type -and $of.Type -ne $nf.Type -and -not ($of.Type -eq 'integer' -and $nf.Type -eq 'number')) {
                $reportedInputs.Add($field)
                foreach ($u in $uses) {
                    $state = Get-SeedFieldState $u.Parameters $field
                    # Aus einem Objekt wird etwa ein String: Setzt der Flow Felder darin, bricht das wie
                    # ein anderer Typ des Feldes selbst.
                    if ($state -eq 'unset' -and (Test-SeedSetsWithin $u.Parameters $field)) { $state = 'within' }
                    switch ($state) {
                        'set' { & $add (Get-SeedBreakingSeverity $u) $id "Typ von $field ändert sich von $($of.Type) zu $($nf.Type); der Flow setzt das Feld." $u }
                        'within' { & $add (Get-SeedBreakingSeverity $u) $id "Typ von $field ändert sich von $($of.Type) zu $($nf.Type); der Flow setzt Felder darin." $u }
                        'unknown' { & $add warning $id "Typ von $field ändert sich von $($of.Type) zu $($nf.Type); der Flow setzt es per Ausdruck, nicht prüfbar." $u }
                    }
                }
            }
            if ($nf.Enum.Count -gt 0 -and $of) {
                $removed = @($of.Enum | Where-Object { $_ -cnotin $nf.Enum })
                $restricted = $of.Enum.Count -eq 0
                if (-not $removed.Count -and -not $restricted) { continue }
                foreach ($u in $uses) {
                    $state = Get-SeedFieldState $u.Parameters $field
                    $values = if ($state -eq 'set') { @($u.Parameters[$field]) } else { @() }
                    $byExpression = $state -eq 'unknown' -or ($values | Where-Object { Test-SeedExpression $_ })
                    foreach ($value in $values | Where-Object { -not (Test-SeedExpression $_) }) {
                        if ([string] $value -cnotin $nf.Enum) {
                            & $add (Get-SeedBreakingSeverity $u) $id "Wert '$(Format-SeedValue $value)' von $field ist nicht mehr erlaubt (erlaubt: $($nf.Enum -join ', '))." $u
                        }
                    }
                    if ($byExpression -and $removed.Count) {
                        & $add warning $id "Werte $($removed -join ', ') von $field entfallen; der Flow setzt das Feld per Ausdruck, nicht prüfbar." $u
                    }
                }
            }
        }

        # Nur das oberste entfallene Feld melden: entfällt body/address, nicht noch body/address/street;
        # ebenso nichts unterhalb eines Feldes mit gemeldetem Typwechsel.
        foreach ($field in Get-SeedKeys $o.Inputs) {
            if ($n.Inputs.Contains($field)) { continue }
            if ($reportedInputs | Where-Object { $field.StartsWith("$_/") -or $field.StartsWith("$($_)[]") }) { continue }
            $reportedInputs.Add($field)
            foreach ($u in $uses) {
                if ((Get-SeedFieldState $u.Parameters $field) -eq 'set') {
                    & $add warning $id "Feld $field entfällt; der Flow setzt es, der Wert kommt nicht mehr an." $u
                }
            }
        }

        # Ebenso bei Antworten: entfällt history, dann nicht noch history[] und history[]/at.
        $reportedOutputs = [Collections.Generic.List[string]]::new()
        foreach ($field in Get-SeedKeys $o.Outputs) {
            $nf = $n.Outputs[$field]
            $of = $o.Outputs[$field]
            $change = if (-not $nf) { 'entfällt' } elseif ($of.Type -and $nf.Type -and $nf.Type -ne $of.Type) { "ändert den Typ von $($of.Type) zu $($nf.Type)" }
            if (-not $change) { continue }
            $plain = ($field -replace '\[\]', '').Trim('/')
            if (-not $plain -or ($reportedOutputs | Where-Object { $plain -eq $_ -or $plain.StartsWith("$_/") })) { continue }
            $reportedOutputs.Add($plain)
            foreach ($u in $uses) {
                $read = @($u.OutputPaths | Where-Object { $_ -eq $plain -or $plain.StartsWith("$_/") -or $_.StartsWith("$plain/") })
                if ($read.Count) { & $add warning $id "Antwortfeld $plain $change; der Flow liest $($read -join ', ')." $u }
            }
        }

        $states[$id] = if ($n.Deprecated) { 'deprecated' } elseif ((Get-SeedSignature $o) -ne (Get-SeedSignature $n)) { 'geändert' } else { 'unverändert' }
    }
    [pscustomobject]@{ States = $states; Findings = $findings }
}

# --- Verbindungen ------------------------------------------------------------------------------------

# Wird eine Verbindung genutzt? Ja, wenn ein Flow sie direkt einbindet (Flows außerhalb von
# Solutions) oder eine Connection Reference auf sie zeigt. $ReferencedIds ist $null, wenn die
# Connection References nicht lesbar sind; dann bleibt „nein“ unbekannt ($null).
function Get-SeedConnectionUse([string] $Connection, $ReferencedIds, [string[]] $FlowConnections = @()) {
    if ($FlowConnections -contains $Connection) { return $true }
    if ($null -eq $ReferencedIds) { return $null }
    @($ReferencedIds) -contains $Connection
}

# Je Verbindung zum Connector: Status und App-Rollen des Kontos. Ungenutzte Verbindungen (Used $false)
# sind nur ein Hinweis; ist die Nutzung unbekannt ($null), eine Warnung. Sind die Rollen eines Kontos
# nicht ermittelbar (Roles '?', meist fehlende Graph-Rechte), gibt es einmal die Warnung
# „Verbindungen nicht prüfbar“ statt eines Hinweises je Verbindung.
function Get-SeedConnectionFindings($Connections, [string] $AppName) {
    $unknown = [Collections.Generic.List[object]]::new()
    foreach ($c in $Connections) {
        $label = "Verbindung $($c.Connection) ($(if ($c.Account) { $c.Account } else { 'Konto unbekannt' }))"
        $severity = if ($c.Used -eq $false) { 'info' } else { 'warning' }
        $unused = if ($c.Used -eq $false) { ' Weder ein Flow noch eine Connection Reference nutzt sie.' } else { '' }
        if ($c.Status -and $c.Status -ne 'Connected') {
            New-SeedFinding $severity '' "$label hat den Status $($c.Status).$unused"
        }
        elseif ($c.Roles -contains '?') {
            $unknown.Add($c)
        }
        elseif (-not ($c.Roles | Where-Object { $_ -and $_ -ne 'Standardzugriff' })) {
            New-SeedFinding $severity '' "${label}: Das Konto hat keine App-Rolle an $AppName; Aufrufe über diese Verbindung scheitern.$unused"
        }
    }
    if ($unknown.Count) {
        $reason = @($unknown | ForEach-Object RoleError | Where-Object { $_ }) | Select-Object -First 1
        New-SeedFinding warning '' "Verbindungen nicht prüfbar: Für $($unknown.Count) Verbindungen sind die App-Rollen der Konten nicht ermittelbar$(if ($reason) { " ($reason)" }). Die Pipeline-Identität braucht dafür Graph-Rechte (README, Prüfung des Custom Connectors)."
    }
}

# --- Zusammenfassung ---------------------------------------------------------------------------------

function Get-SeedFlowLink([string] $EnvironmentId, [string] $ResourceId) {
    # Power Automate führt Flows unter ihrer resourceid (nicht workflowid); Entwürfe, die nie aktiv
    # waren, haben keine und bekommen keinen Link.
    if ($EnvironmentId -match '^[A-Za-z0-9-]+$' -and $ResourceId -match '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$') {
        "https://make.powerautomate.com/environments/$EnvironmentId/flows/$ResourceId/details"
    }
}

function Format-SeedFlowCell([string] $Name, [string] $Link) {
    if (-not $Name) { return '' }
    if ($Link) { "[$(Format-SeedMarkdown $Name)]($Link)" } else { Format-SeedMarkdown $Name }
}

# Markdown für ##vso[task.uploadsummary]: Connector, Operationen mit Stand, Befunde, Flows,
# Verbindungen und welche Prüfungen gelaufen sind.
function New-SeedConnectorSummary {
    param(
        [string] $DisplayName, [string] $InternalId, [string] $EnvironmentUrl, [string] $EnvironmentId, [string] $Backend,
        $States, $OldOperations, $NewOperations, [object[]] $Usages = @(), [object[]] $Findings = @(),
        [string] $FlowCheck, [string] $ConnectionCheck, [object[]] $Connections = @(), [bool] $Overridden
    )
    $errors = @($Findings | Where-Object Severity -eq 'error').Count
    $warnings = @($Findings | Where-Object Severity -eq 'warning').Count
    $label = @{ error = 'Fehler'; warning = 'Warnung'; info = 'Hinweis' }
    $md = [Collections.Generic.List[string]]::new()
    $md.Add("# Custom Connector: $(Format-SeedMarkdown $DisplayName)")
    $md.Add('')
    $md.Add("**$errors Fehler, $warnings Warnungen**$(if ($Overridden) { ' (Fehler übersteuert mit allowBreakingConnectorChanges)' }) · Umgebung $(Format-SeedMarkdown $EnvironmentUrl) · ``$(Format-SeedLogText $InternalId)``")
    if ($Backend) { $md.Add(''); $md.Add("Backend bisher: $(Format-SeedMarkdown $Backend)") }
    $md.Add('')
    $md.Add('## Prüfungen')
    $md.Add('')
    $md.Add("- Nutzung in Flows: $FlowCheck")
    $md.Add("- Verbindungen und App-Rollen: $ConnectionCheck")

    if ($States -and @(Get-SeedKeys $States).Count) {
        $md.Add('')
        $md.Add('## Operationen')
        $md.Add('')
        $md.Add('| Operation | Stand | Genutzt in Actions | Methode und Pfad |')
        $md.Add('| --- | --- | --- | --- |')
        foreach ($id in Get-SeedKeys $States) {
            $op = $NewOperations[$id] ?? $OldOperations[$id]
            $count = @($Usages | Where-Object { $_.OperationId -ceq $id }).Count
            $md.Add("| $(Format-SeedMarkdown $id) | $($States[$id]) | $count | $($op.Method) $(Format-SeedMarkdown $op.Path) |")
        }
    }

    $md.Add('')
    $md.Add('## Befunde')
    $md.Add('')
    if (-not $Findings.Count) { $md.Add('Keine.') }
    else {
        $md.Add('| Schwere | Operation | Befund | Flow | Status | Besitzer | Action |')
        $md.Add('| --- | --- | --- | --- | --- | --- | --- |')
        foreach ($f in $Findings | Sort-Object { @{ error = 0; warning = 1; info = 2 }[$_.Severity] }, Operation, ActionPath, Message) {
            $flow = Format-SeedFlowCell $f.Flow (Get-SeedFlowLink $EnvironmentId $f.ResourceId)
            $md.Add("| $($label[$f.Severity]) | $(Format-SeedMarkdown $f.Operation) | $(Format-SeedMarkdown $f.Message) | $flow | $($f.State) | $(Format-SeedMarkdown $f.Owner) | $(Format-SeedMarkdown $f.ActionPath) |")
        }
    }

    if (@($Usages).Count) {
        $md.Add('')
        $md.Add('## Flows')
        $md.Add('')
        $md.Add('| Flow | Status | Besitzer | Operationen |')
        $md.Add('| --- | --- | --- | --- |')
        foreach ($group in $Usages | Group-Object FlowId) {
            $u = $group.Group[0]
            $operations = ($group.Group.OperationId | Select-Object -Unique | ForEach-Object { Format-SeedMarkdown $_ }) -join ', '
            $md.Add("| $(Format-SeedFlowCell $u.Flow (Get-SeedFlowLink $EnvironmentId $u.ResourceId)) | $($u.State) | $(Format-SeedMarkdown $u.Owner) | $operations |")
        }
    }

    if (@($Connections).Count) {
        $md.Add('')
        $md.Add('## Verbindungen')
        $md.Add('')
        $md.Add('| Verbindung | Konto | Status | Genutzt | App-Rollen |')
        $md.Add('| --- | --- | --- | --- | --- |')
        foreach ($c in $Connections) {
            $used = if ($c.Used -eq $true) { 'ja' } elseif ($c.Used -eq $false) { 'nein' } else { '?' }
            $md.Add("| $(Format-SeedMarkdown $c.Connection) | $(Format-SeedMarkdown $c.Account) | $(Format-SeedMarkdown $c.Status) | $used | $(Format-SeedMarkdown (@($c.Roles) -join ', ')) |")
        }
        $md.Add('')
        $md.Add('Konto: das angemeldete Konto der Verbindung; nennt sie keins (bei OAuth die Regel), die Person, die sie angelegt hat.')
    }
    , $md.ToArray()
}

# --- Wiederholen bei vorübergehenden Fehlern ---------------------------------------------------------

# Wartezeit vor einem neuen Versuch: nur bei 429 (Dataverse drosselt) und 502, 503, 504, nach
# Retry-After, sonst 5 Sekunden, höchstens 30; bei anderen Fehlern $null.
function Get-SeedRetryDelay($ErrorRecord) {
    $response = $ErrorRecord.Exception.Response
    if ($response -isnot [Net.Http.HttpResponseMessage] -or [int] $response.StatusCode -notin 429, 502, 503, 504) { return $null }
    $after = $response.Headers.RetryAfter
    # PowerShell packt Nullable<T> selbst aus: Delta ist ein TimeSpan oder $null.
    $seconds = if ($null -ne $after.Delta) { $after.Delta.TotalSeconds } elseif ($null -ne $after.Date) { ($after.Date - [DateTimeOffset]::UtcNow).TotalSeconds } else { 5 }
    [int] [Math]::Min(30, [Math]::Max(1, [Math]::Ceiling($seconds)))
}

# Führt $Request aus und wiederholt ihn bei vorübergehenden Fehlern höchstens $Retries-mal.
function Invoke-SeedRetry([scriptblock] $Request, [int] $Retries = 2, [scriptblock] $Wait = { param($Seconds) Start-Sleep -Seconds $Seconds }) {
    for ($attempt = 0; ; $attempt++) {
        try { return & $Request }
        catch {
            $delay = Get-SeedRetryDelay $_
            if ($null -eq $delay -or $attempt -ge $Retries) { throw }
            & $Wait $delay
        }
    }
}

Export-ModuleMember -Function *-Seed*
