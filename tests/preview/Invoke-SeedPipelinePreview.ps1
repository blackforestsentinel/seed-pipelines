#requires -Version 7
<#
.SYNOPSIS
    Vorschau einer Projekt-Pipeline mit den Templates aus seed-pipelines, ohne Lauf.

.DESCRIPTION
    Schickt eine YAML-Datei als yamlOverride an die Vorschau-API von Azure DevOps
    (POST _apis/pipelines/<id>/preview mit previewRun). Azure DevOps löst dabei Templates und
    Template-Ausdrücke auf und liefert das fertige YAML zurück. Es entsteht kein Lauf, es gibt
    keine Freigabe, und keine Service Connection wird benutzt; die Vorschau liest nur.

    Die Pipeline-ID ist nur der Rahmen: Projekt, Repository und GitHub-Service-Connection. Ihre
    eigene YAML-Datei spielt keine Rolle. Service Connections und Environments, die das YAML
    nennt, müssen im Projekt existieren, sonst lehnt Azure DevOps schon die Vorschau ab.

    Das Ergebnis landet neben der YAML-Datei als <name>.final.yml. Mit -ExpectBuildSteps prüft
    das Skript, dass diese Schritte (displayName) im Job build in genau dieser Reihenfolge
    vorkommen, mit -AbsentBuildSteps, dass sie dort fehlen; -ExpectDeploySteps und
    -AbsentDeploySteps prüfen ebenso die erste Stage deploy_<umgebung>. Bei Abweichungen endet es
    mit Fehler.

    Ausführen mit einem az-Login, der die Pipeline lesen darf. Mit -FinalYamlPath prüft das Skript
    stattdessen ein schon gespeichertes <name>.final.yml, ohne Azure DevOps.

.PARAMETER SeedRef
    Ersetzt die ref der Repository-Ressource (erste Zeile "ref:" im YAML), etwa
    refs/heads/<branch>, um einen Branch von seed-pipelines vor dem Release zu prüfen. Der
    Branch muss auf GitHub liegen.

.PARAMETER Replace
    Platzhalter im YAML, z. B. @{ SERVICE_CONNECTION = 'sc-demo' } ersetzt __SERVICE_CONNECTION__.
    Die Beispiele in diesem Ordner nennen ihre Platzhalter im Kopf.

.PARAMETER TemplateParameters
    Laufzeitparameter des YAML, z. B. @{ confirmDataDeletion = 'true' }.

.PARAMETER FinalYamlPath
    Ein gespeichertes Ergebnis (<name>.final.yml) nur prüfen, ohne Vorschau, etwa nach geänderten
    Erwartungen.

.EXAMPLE
    ./Invoke-SeedPipelinePreview.ps1 -Organization https://dev.azure.com/org -Project Apps -PipelineId 12 `
        -YamlPath ./extra-build-steps.yml -SeedRef refs/heads/feature `
        -Replace @{ GITHUB_CONNECTION = 'github-org'; SERVICE_CONNECTION = 'sc-apps'; PROJECT = 'apps'; ENVIRONMENT = 'dev' } `
        -ExpectBuildSteps 'npm run build', 'Lint (Beispiel)', 'Artefakt web'

.EXAMPLE
    ./Invoke-SeedPipelinePreview.ps1 -FinalYamlPath ./extension-signing.final.yml `
        -ExpectDeploySteps 'Browser-Erweiterung packen', 'Erweiterung signieren und veröffentlichen'
#>
[CmdletBinding(DefaultParameterSetName = 'Preview')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Preview')] [string] $Organization,
    [Parameter(Mandatory, ParameterSetName = 'Preview')] [string] $Project,
    [Parameter(Mandatory, ParameterSetName = 'Preview')] [int] $PipelineId,
    [Parameter(Mandatory, ParameterSetName = 'Preview')] [string] $YamlPath,
    [Parameter(ParameterSetName = 'Preview')] [string] $SeedRef = '',
    [Parameter(ParameterSetName = 'Preview')] [hashtable] $Replace = @{},
    [Parameter(ParameterSetName = 'Preview')] [hashtable] $TemplateParameters = @{},
    [Parameter(Mandatory, ParameterSetName = 'Saved')] [string] $FinalYamlPath,
    [string[]] $ExpectBuildSteps = @(),
    [string[]] $AbsentBuildSteps = @(),
    [string[]] $ExpectDeploySteps = @(),
    [string[]] $AbsentDeploySteps = @()
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

# displayName der Schritte einer Stage in Reihenfolge. Das fertige YAML von Azure DevOps ist
# einheitlich formatiert: Schritte stehen auf der Höhe ihres steps:, ihr displayName zwei Zeichen
# tiefer. Tiefer eingerückte Zeilen gehören zu Skripten und Eingaben, nicht zum Schritt.
function Get-StageSteps([string[]] $Lines, [string] $StagePattern) {
    $start = [array]::FindIndex($Lines, [Predicate[string]] { param($l) $l -match "^\s*- stage: $StagePattern\s*$" })
    if ($start -lt 0) { return $null }
    $steps = [Collections.Generic.List[string]]::new()
    $indent = -1
    for ($i = $start + 1; $i -lt $Lines.Count -and $Lines[$i] -notmatch '^\s*- stage: '; $i++) {
        # Erst ab steps:, sonst kämen die Namen von Stage und Job mit.
        if ($Lines[$i] -match '^(\s+)steps:\s*$') { $indent = $Matches[1].Length; continue }
        if ($indent -ge 0 -and $Lines[$i] -match "^ {$($indent + 2)}displayName: (.+)$") {
            $steps.Add($Matches[1].Trim().Trim("'", '"'))
        }
    }
    , $steps
}

# Erwartete Schritte in dieser Reihenfolge (dazwischen dürfen andere stehen), fehlende gar nicht.
function Test-Steps([string] $Title, $Steps, [string[]] $Expect, [string[]] $Absent, $Errors) {
    $position = -1
    foreach ($expected in $Expect) {
        $index = -1
        for ($i = $position + 1; $i -lt $Steps.Count; $i++) {
            if ($Steps[$i] -eq $expected) { $index = $i; break }
        }
        if ($index -lt 0) {
            $Errors.Add("${Title}: Schritt '$expected' fehlt oder steht nicht nach dem vorigen erwarteten Schritt.")
        } else {
            $position = $index
        }
    }
    foreach ($name in $Absent) {
        if ($Steps -contains $name) { $Errors.Add("${Title}: Schritt '$name' sollte fehlen.") }
    }
}

if ($PSCmdlet.ParameterSetName -eq 'Saved') {
    $finalYaml = Get-Content -Raw -Path $FinalYamlPath
    Write-Host "Gespeichertes Ergebnis: $FinalYamlPath"
} else {
    $yaml = Get-Content -Raw -Path $YamlPath
    foreach ($key in $Replace.Keys) {
        $yaml = $yaml.Replace("__$($key)__", [string] $Replace[$key])
    }
    if ($SeedRef) {
        $rx = [regex] '(?m)^(\s+ref:\s*)\S+'
        if (-not $rx.IsMatch($yaml)) { throw "$YamlPath enthält keine Zeile 'ref:' für -SeedRef." }
        $yaml = $rx.Replace($yaml, "`${1}$SeedRef", 1)
    }
    # Nur ein Hinweis: Auch Templates enthalten solche Namen absichtlich (__API_ORIGIN__ ersetzt erst
    # der Deploy).
    $left = [regex]::Matches($yaml, '__[A-Z_]+__') | ForEach-Object Value | Sort-Object -Unique
    if ($left) { Write-Warning "Nicht ersetzt (-Replace), falls Platzhalter gemeint sind: $($left -join ', ')" }

    $body = @{ previewRun = $true; yamlOverride = $yaml }
    if ($TemplateParameters.Count) { $body.templateParameters = $TemplateParameters }

    $token = az account get-access-token --resource 499b84ac-1321-427f-aa17-267ca6975798 --query accessToken -o tsv
    $url = "$($Organization.TrimEnd('/'))/$([uri]::EscapeDataString($Project))/_apis/pipelines/$PipelineId/preview?api-version=7.1"
    try {
        $result = Invoke-RestMethod -Method Post -Uri $url -Headers @{ Authorization = "Bearer $token" } `
            -ContentType 'application/json' -Body (ConvertTo-Json $body -Depth 10)
    } catch {
        $detail = $_.ErrorDetails.Message
        $message = try { (ConvertFrom-Json $detail).message } catch { $detail }
        throw "Vorschau abgelehnt: $message"
    }

    $final = Join-Path (Split-Path -Parent (Resolve-Path $YamlPath)) "$([IO.Path]::GetFileNameWithoutExtension($YamlPath)).final.yml"
    $finalYaml = $result.finalYaml
    Set-Content -Path $final -Value $finalYaml -Encoding utf8NoBOM
    Write-Host "Vorschau ok, aufgelöstes YAML: $final"
}

$lines = $finalYaml -split "`r?`n"
$errors = [Collections.Generic.List[string]]::new()
$steps = Get-StageSteps $lines 'build'
if ($null -eq $steps) { throw 'Keine Stage build im aufgelösten YAML.' }
Write-Host "Schritte im Build ($($steps.Count)):"
$steps | ForEach-Object { Write-Host "  - $_" }
Test-Steps 'Build' $steps $ExpectBuildSteps $AbsentBuildSteps $errors

# Erste Umgebung; PR-Läufe und validationOnly haben keine.
$deploySteps = Get-StageSteps $lines 'deploy_\S+'
if ($null -ne $deploySteps) {
    Write-Host "Schritte im ersten Deploy ($($deploySteps.Count)):"
    $deploySteps | ForEach-Object { Write-Host "  - $_" }
    Test-Steps 'Deploy' $deploySteps $ExpectDeploySteps $AbsentDeploySteps $errors
} elseif ($ExpectDeploySteps.Count) {
    $errors.Add('Keine Stage deploy_<umgebung> im aufgelösten YAML.')
}
$stages = $lines | Where-Object { $_ -match '^\s*- stage: (\S+)' } | ForEach-Object { ($_ -split ':')[1].Trim() }
Write-Host "Stages: $($stages -join ', ')"

if ($errors.Count) {
    $errors | ForEach-Object { Write-Host "  [fehler] $_" -ForegroundColor Red }
    throw "$($errors.Count) Abweichungen in der Vorschau."
}
if ($ExpectBuildSteps.Count -or $AbsentBuildSteps.Count -or $ExpectDeploySteps.Count -or $AbsentDeploySteps.Count) {
    Write-Host 'Reihenfolge und fehlende Schritte wie erwartet.' -ForegroundColor Green
}
