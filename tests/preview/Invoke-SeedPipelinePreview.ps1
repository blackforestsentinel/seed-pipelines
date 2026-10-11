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
    vorkommen, mit -AbsentBuildSteps, dass sie dort fehlen. Bei Abweichungen endet es mit Fehler.

    Ausführen mit einem az-Login, der die Pipeline lesen darf.

.PARAMETER SeedRef
    Ersetzt die ref der Repository-Ressource (erste Zeile "ref:" im YAML), etwa
    refs/heads/<branch>, um einen Branch von seed-pipelines vor dem Release zu prüfen. Der
    Branch muss auf GitHub liegen.

.PARAMETER Replace
    Platzhalter im YAML, z. B. @{ SERVICE_CONNECTION = 'sc-demo' } ersetzt __SERVICE_CONNECTION__.
    Die Beispiele in diesem Ordner nennen ihre Platzhalter im Kopf.

.PARAMETER TemplateParameters
    Laufzeitparameter des YAML, z. B. @{ confirmDataDeletion = 'true' }.

.EXAMPLE
    ./Invoke-SeedPipelinePreview.ps1 -Organization https://dev.azure.com/org -Project Apps -PipelineId 12 `
        -YamlPath ./extra-build-steps.yml -SeedRef refs/heads/feature `
        -Replace @{ GITHUB_CONNECTION = 'github-org'; SERVICE_CONNECTION = 'sc-apps'; PROJECT = 'apps'; ENVIRONMENT = 'dev' } `
        -ExpectBuildSteps 'npm run build', 'Lint (Beispiel)', 'Artefakt web'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Organization,
    [Parameter(Mandatory)] [string] $Project,
    [Parameter(Mandatory)] [int] $PipelineId,
    [Parameter(Mandatory)] [string] $YamlPath,
    [string] $SeedRef = '',
    [hashtable] $Replace = @{},
    [hashtable] $TemplateParameters = @{},
    [string[]] $ExpectBuildSteps = @(),
    [string[]] $AbsentBuildSteps = @()
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

$yaml = Get-Content -Raw -Path $YamlPath
foreach ($key in $Replace.Keys) {
    $yaml = $yaml.Replace("__$($key)__", [string] $Replace[$key])
}
if ($SeedRef) {
    $rx = [regex] '(?m)^(\s+ref:\s*)\S+'
    if (-not $rx.IsMatch($yaml)) { throw "$YamlPath enthält keine Zeile 'ref:' für -SeedRef." }
    $yaml = $rx.Replace($yaml, "`${1}$SeedRef", 1)
}
$left = [regex]::Matches($yaml, '__[A-Z_]+__') | ForEach-Object Value | Sort-Object -Unique
if ($left) { throw "Platzhalter ohne Wert (-Replace): $($left -join ', ')" }

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
Set-Content -Path $final -Value $result.finalYaml -Encoding utf8NoBOM
Write-Host "Vorschau ok, aufgelöstes YAML: $final"

# Schritte des Jobs build in Reihenfolge: alle displayName zwischen "- stage: build" und der
# nächsten Stage. Das fertige YAML von Azure DevOps ist einheitlich formatiert.
$lines = $result.finalYaml -split "`r?`n"
$start = [array]::FindIndex($lines, [Predicate[string]] { param($l) $l -match '^\s*- stage: build\s*$' })
if ($start -lt 0) { throw 'Keine Stage build im aufgelösten YAML.' }
$steps = [Collections.Generic.List[string]]::new()
$inSteps = $false
for ($i = $start + 1; $i -lt $lines.Count -and $lines[$i] -notmatch '^\s*- stage: '; $i++) {
    # Erst ab steps:, sonst kämen die Namen von Stage und Job mit.
    if ($lines[$i] -match '^\s+steps:\s*$') { $inSteps = $true; continue }
    if ($inSteps -and $lines[$i] -match '^\s+displayName: (.+)$') {
        $steps.Add($Matches[1].Trim().Trim("'", '"'))
    }
}
Write-Host "Schritte im Build ($($steps.Count)):"
$steps | ForEach-Object { Write-Host "  - $_" }

$errors = [Collections.Generic.List[string]]::new()
$position = -1
foreach ($expected in $ExpectBuildSteps) {
    $index = -1
    for ($i = $position + 1; $i -lt $steps.Count; $i++) {
        if ($steps[$i] -eq $expected) { $index = $i; break }
    }
    if ($index -lt 0) {
        $errors.Add("Schritt '$expected' fehlt oder steht nicht nach dem vorigen erwarteten Schritt.")
    } else {
        $position = $index
    }
}
foreach ($absent in $AbsentBuildSteps) {
    if ($steps -contains $absent) { $errors.Add("Schritt '$absent' sollte im Build fehlen.") }
}
$stages = $lines | Where-Object { $_ -match '^\s*- stage: (\S+)' } | ForEach-Object { ($_ -split ':')[1].Trim() }
Write-Host "Stages: $($stages -join ', ')"

if ($errors.Count) {
    $errors | ForEach-Object { Write-Host "  [fehler] $_" -ForegroundColor Red }
    throw "$($errors.Count) Abweichungen in der Vorschau."
}
if ($ExpectBuildSteps.Count -or $AbsentBuildSteps.Count) {
    Write-Host 'Reihenfolge und fehlende Schritte wie erwartet.' -ForegroundColor Green
}
