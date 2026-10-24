#requires -Version 7
<#
.SYNOPSIS
    Sentinel Seed: Signaturschlüssel einer Browser-Erweiterung anlegen und als Secure File hinterlegen.

.DESCRIPTION
    Für extension.signing in web-app.yml (README, Browser-Erweiterung): Der Deploy signiert das
    Paket mit diesem Schlüssel, und aus dem Schlüssel folgt die Erweiterungs-ID. Jeder Schritt prüft
    zuerst und legt nur an, was fehlt; das Skript lässt sich also gefahrlos wiederholen.

      1. Schlüssel: erzeugt lokal einen RSA-Schlüssel (2048 Bit, PEM, PKCS#8) in -KeyFile oder liest
         mit -ExistingKey einen vorhandenen. Die ID rechnet das Skript wie
         scripts/extension/publish.py aus dem öffentlichen Schlüssel.
      2. Secure File -SecureFileName im Azure-DevOps-Projekt mit diesem Schlüssel; die ID steht als
         Eigenschaft extensionId daran. Gibt es die Secure File schon, lädt das Skript nichts hoch
         und erzeugt keinen neuen Schlüssel: Mit -ExistingKey prüft es, dass die Eigenschaft zur ID
         des Schlüssels passt, sonst weist es nur darauf hin.
      3. Berechtigung der Pipeline -PipelineName für die Secure File, damit ihr erster Lauf nicht
         nach Zugriff fragt.

    Am Ende nennt das Skript die Werte für extension.signing in azure-pipelines.yml und für
    project.yaml (auth.extraRedirectUris, hosting.corsAllowedOrigins).

    Der private Schlüssel erscheint in keiner Ausgabe; er geht nur als Datei an Azure DevOps.
    Danach gehört er sicher offline abgelegt (Passwortmanager, Tresor): Azure DevOps gibt Secure
    Files nicht wieder heraus, und ein neuer Schlüssel ergibt eine neue ID, also für Browser eine
    neue Erweiterung.

    Ausführen mit einem az-Login, der im Projekt Secure Files anlegen und Pipelines dafür
    berechtigen darf (Projektadministrator oder Administrator der Secure Files).

.PARAMETER KeyFile
    Datei des privaten Schlüssels. Ohne -ExistingKey legt das Skript sie neu an und bricht ab, wenn
    es sie schon gibt.

.PARAMETER ExistingKey
    -KeyFile ist ein vorhandener Schlüssel (RSA, PEM, unverschlüsselt), etwa aus dem Passwortmanager:
    hochladen, falls die Secure File fehlt, sonst gegen sie prüfen.

.PARAMETER SecureFileName
    Name der Secure File, wie extension.signing.secureFile; ohne Angabe der Dateiname von -KeyFile.

.PARAMETER PipelineName
    Pipeline des Projekts, die die Secure File nutzt.

.PARAMETER DryRun
    Nur prüfen und anzeigen, was angelegt würde. Legt weder Schlüssel noch Secure File an.

.EXAMPLE
    ./New-SeedExtensionKey.ps1 -KeyFile C:\keys\my-extension.pem -AzureDevOpsOrganization https://dev.azure.com/contoso `
        -AzureDevOpsProject Apps -PipelineName my-app -DryRun

.EXAMPLE
    ./New-SeedExtensionKey.ps1 -KeyFile C:\keys\my-extension.pem -ExistingKey -AzureDevOpsOrganization https://dev.azure.com/contoso `
        -AzureDevOpsProject Apps -PipelineName my-app
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $KeyFile,
    [switch] $ExistingKey,
    [Parameter(Mandatory)] [string] $AzureDevOpsOrganization,
    [Parameter(Mandatory)] [string] $AzureDevOpsProject,
    [string] $SecureFileName = '',
    [Parameter(Mandatory)] [string] $PipelineName,
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true

# .NET liest Pfade relativ zum Prozess, nicht zum aktuellen PowerShell-Ordner.
$KeyFile = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($KeyFile)
if (-not $SecureFileName) { $SecureFileName = Split-Path -Leaf $KeyFile }

$Org = $AzureDevOpsOrganization.TrimEnd('/')
if ($Org -notmatch '^https://dev\.azure\.com/[^/]+$' -and $Org -notmatch '^https://[^./]+\.visualstudio\.com$') {
    throw 'AzureDevOpsOrganization: https://dev.azure.com/<organisation> erwartet.'
}
$AzureDevOpsResource = '499b84ac-1321-427f-aa17-267ca6975798'
$IdProperty = 'extensionId'

$work = New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) "seed-extension-key-$(Get-Random)")
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

function Invoke-AzDevOps([string] $Method, [string] $Url, [object] $Body, [string] $InFile, [string] $ContentType = 'application/json') {
    # Direkt per HTTP: URLs mit & überstehen den Umweg über az.cmd nicht. Bodies gehen als Datei
    # raus, der Schlüssel als die Datei selbst; nichts davon landet in einer Ausgabe.
    if (-not $script:token) {
        $script:token = az account get-access-token --resource $AzureDevOpsResource --query accessToken -o tsv
    }
    $request = @{ Method = $Method; Uri = $Url; Headers = @{ Authorization = "Bearer $script:token" }; ContentType = $ContentType }
    if ($InFile) { $request.InFile = $InFile }
    elseif ($null -ne $Body) { $request.InFile = Save-Json $Body "body-$(Get-Random).json" }
    Invoke-RestMethod @request
}

# Erweiterungs-ID wie in Chromium und scripts/extension/publish.py: SHA-256 über den öffentlichen
# Schlüssel (SubjectPublicKeyInfo, DER), die ersten 16 Byte als Buchstaben a bis p.
function Get-ExtensionId([Security.Cryptography.RSA] $Rsa) {
    $hash = [Security.Cryptography.SHA256]::HashData($Rsa.ExportSubjectPublicKeyInfo())
    -join ($hash[0..15] | ForEach-Object { [char](97 + ($_ -shr 4)); [char](97 + ($_ -band 15)) })
}

# Privater RSA-Schlüssel aus einer PEM-Datei (PKCS#8 oder PKCS#1, unverschlüsselt), wie openssl
# ihn im Deploy liest. Fehlermeldungen nennen nie den Inhalt.
function Read-PrivateKey([string] $Path) {
    $text = [IO.File]::ReadAllText($Path)
    if ($text -match '-----BEGIN ENCRYPTED PRIVATE KEY-----') {
        throw "$Path ist verschlüsselt. Die Pipeline braucht den Schlüssel ohne Passwort (openssl pkey -in <datei> -out <neu>)."
    }
    if ($text -notmatch '-----BEGIN (RSA )?PRIVATE KEY-----') {
        throw "$Path enthält keinen privaten Schlüssel im PEM-Format (BEGIN PRIVATE KEY oder BEGIN RSA PRIVATE KEY)."
    }
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $rsa.ImportFromPem($text)
    } catch {
        $rsa.Dispose()
        throw "$Path ist kein RSA-Schlüssel, den .NET lesen kann ($($_.Exception.InnerException.Message ?? $_.Exception.Message))."
    }
    if ($rsa.KeySize -lt 2048) {
        $size = $rsa.KeySize
        $rsa.Dispose()
        throw "$Path hat nur $size Bit, erwartet mindestens 2048."
    }
    $rsa
}

# Neuer Schlüssel in eine neue Datei. CreateNew bricht ab, statt eine vorhandene Datei zu
# überschreiben. Lesen darf sie nur der eigene Benutzer, und zwar ab dem Anlegen, also bevor der
# Schlüssel darin steht: außerhalb von Windows per Dateimodus 600, unter Windows mit einer eigenen
# Zugriffsliste ohne geerbte Einträge (nur der eigene Benutzer, Vollzugriff). Sonst erbte die Datei
# die Rechte des Ordners, in dem etwa Administratoren oder andere Benutzer lesen dürfen.
function New-PrivateKey([string] $Path) {
    $rsa = [Security.Cryptography.RSA]::Create(2048)
    if ($IsWindows) {
        $security = [Security.AccessControl.FileSecurity]::new()
        $security.SetAccessRuleProtection($true, $false)
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'Allow'))
        $stream = [IO.FileSystemAclExtensions]::Create([IO.FileInfo]::new($Path), [IO.FileMode]::CreateNew,
            [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    } else {
        $stream = [IO.FileStream]::new($Path, [IO.FileStreamOptions] @{
                Mode = 'CreateNew'; Access = 'Write'; Share = 'None'; UnixCreateMode = [IO.UnixFileMode] 'UserRead, UserWrite' })
    }
    $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
    try {
        $writer.Write($rsa.ExportPkcs8PrivateKeyPem())
    } finally {
        $writer.Dispose()
    }
    $rsa
}

try {
    $project = Invoke-AzDevOps get "$Org/_apis/projects/$([uri]::EscapeDataString($AzureDevOpsProject))?api-version=7.1"
    $api = "$Org/$($project.id)/_apis"
    function Get-SecureFile {
        (Invoke-AzDevOps get "$api/distributedtask/securefiles?api-version=7.1-preview.1").value |
            Where-Object name -eq $SecureFileName | Select-Object -First 1
    }
    $secureFile = Get-SecureFile
    $definition = (Invoke-AzDevOps get "$api/build/definitions?name=$([uri]::EscapeDataString($PipelineName))&api-version=7.1").value |
        Where-Object name -eq $PipelineName | Select-Object -First 1

    # Vor jeder Änderung: Eine vorhandene Datei wird nie überschrieben.
    if (-not $ExistingKey -and -not $secureFile -and (Test-Path -LiteralPath $KeyFile)) {
        throw "$KeyFile gibt es schon. Einen vorhandenen Schlüssel mit -ExistingKey hochladen, sonst eine neue Datei nennen."
    }
    if ($ExistingKey -and -not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) {
        throw "$KeyFile gibt es nicht (-ExistingKey)."
    }

    Write-Host "Sentinel Seed Signaturschlüssel der Browser-Erweiterung$(if ($DryRun) { ' (DryRun, keine Änderungen)' })"
    Write-Host "  Azure DevOps $Org/$($project.name), Secure File $SecureFileName, Pipeline $PipelineName"
    Write-Host ''

    # --- 1. Schlüssel ---------------------------------------------------------------------
    Write-Host '1. Schlüssel'
    $extensionId = $null
    $keyInHand = $false
    if ($ExistingKey) {
        $rsa = Read-PrivateKey $KeyFile
        try { $extensionId = Get-ExtensionId $rsa } finally { $rsa.Dispose() }
        $keyInHand = $true
        Write-Host "  [ok]    $KeyFile, ID $extensionId"
    } elseif ($secureFile) {
        $extensionId = $secureFile.properties.$IdProperty
        Write-Host "          Kein neuer Schlüssel: Die Secure File $SecureFileName gibt es schon$(if ($extensionId) { " (ID $extensionId)" })."
        Write-Host '          Ein neuer Schlüssel ergäbe eine neue ID. Zum Prüfen eines vorhandenen Schlüssels mit'
        Write-Host '          -ExistingKey ausführen; zum Ersetzen die Secure File erst in Azure DevOps löschen.'
    } else {
        $rsa = Invoke-Step "Schlüssel $KeyFile (RSA 2048, PKCS#8)" { $null } { New-PrivateKey $KeyFile }
        if ($rsa) {
            try { $extensionId = Get-ExtensionId $rsa } finally { $rsa.Dispose() }
            $keyInHand = $true
            Write-Host "          ID $extensionId"
        }
    }

    # --- 2. Secure File -------------------------------------------------------------------
    Write-Host '2. Secure File'
    if ($secureFile) {
        $stored = $secureFile.properties.$IdProperty
        if (-not $ExistingKey) {
            Write-Host "  [ok]    Secure File $SecureFileName$(if ($stored) { ", ID $stored" } else { ' (ohne Eigenschaft extensionId)' })"
        } elseif (-not $stored) {
            Write-Host "  [ok]    Secure File $SecureFileName"
            Write-Host "          Nicht prüfbar: Die Secure File hat keine Eigenschaft $IdProperty (von Hand hochgeladen?)."
            Write-Host '          Der erste signierte Deploy prüft den Schlüssel gegen extension.signing.id.'
        } elseif ($stored -ne $extensionId) {
            throw "Die Secure File $SecureFileName gehört zur ID $stored, der Schlüssel in $KeyFile ergibt $extensionId."
        } else {
            Write-Host "  [ok]    Secure File $SecureFileName passt zum Schlüssel (ID $extensionId)"
        }
    } elseif (-not $keyInHand) {
        Write-Missing "Secure File $SecureFileName" 'Der Schlüssel fehlt noch (DryRun).'
    } else {
        $secureFile = Invoke-Step "Secure File $SecureFileName (ID $extensionId)" { $null } {
            $created = Invoke-AzDevOps post "$api/distributedtask/securefiles?name=$([uri]::EscapeDataString($SecureFileName))&api-version=7.1-preview.1" `
                -InFile $KeyFile -ContentType 'application/octet-stream'
            # Die ID an der Secure File macht spätere Läufe mit -ExistingKey prüfbar; ohne sie geht
            # es auch, deshalb nur eine Warnung.
            try {
                Invoke-AzDevOps patch "$api/distributedtask/securefiles/$($created.id)?api-version=7.1-preview.1" @{
                    id = $created.id; name = $created.name; properties = @{ $IdProperty = $extensionId }
                } | Out-Null
            } catch {
                Write-Warning "Eigenschaft $IdProperty an der Secure File nicht gesetzt: $($_.Exception.Message)"
            }
            Get-SecureFile
        }
    }

    # --- 3. Berechtigung --------------------------------------------------------------------
    Write-Host '3. Berechtigung der Pipeline'
    $title = "Pipeline $PipelineName berechtigt für Secure File $SecureFileName"
    if (-not $definition) {
        Write-Missing $title "Pipeline $PipelineName gibt es im Projekt nicht (erst Initialize-SeedProject.ps1 bzw. seed-scaffold)."
    } elseif (-not $secureFile) {
        Write-Missing $title 'Die Secure File fehlt noch.'
    } else {
        $permissionsUrl = "$api/pipelines/pipelinePermissions/securefile/$($secureFile.id)?api-version=7.1-preview.1"
        Invoke-Step $title {
            (Invoke-AzDevOps get $permissionsUrl).pipelines | Where-Object { $_.id -eq $definition.id -and $_.authorized }
        } {
            Invoke-AzDevOps patch $permissionsUrl @{ pipelines = @(@{ id = $definition.id; authorized = $true }) } | Out-Null
            $true
        } | Out-Null
    }

    # --- Ergebnis ---------------------------------------------------------------------------
    Write-Host ''
    if ($missing.Count) {
        Write-Host "Es fehlen $($missing.Count) Schritte:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "  - $_" }
    } else {
        Write-Host 'Fertig.' -ForegroundColor Green
    }
    if ($extensionId) {
        Write-Host ''
        Write-Host 'Werte für azure-pipelines.yml (Parameter extension):'
        Write-Host '    signing:'
        Write-Host "      secureFile: $SecureFileName"
        Write-Host "      id: $extensionId"
        Write-Host 'Werte für project.yaml, je Umgebung aus signing.environments (Standard prod). Die Listen dort'
        Write-Host 'ersetzen die allgemeinen; weitere Redirect-URIs und Origins mit aufführen:'
        Write-Host '  environments:'
        Write-Host '    prod:'
        Write-Host '      auth:'
        Write-Host "        extraRedirectUris: [https://$extensionId.chromiumapp.org/]"
        Write-Host '      hosting:'
        Write-Host "        corsAllowedOrigins: [chrome-extension://$extensionId]"
    }
    if ($keyInHand) {
        Write-Host ''
        Write-Host "Den Schlüssel $KeyFile jetzt sicher offline ablegen (Passwortmanager, Tresor) und die Datei" -ForegroundColor Yellow
        Write-Host 'hier danach löschen. Azure DevOps gibt Secure Files nicht wieder heraus, und ein neuer Schlüssel' -ForegroundColor Yellow
        Write-Host 'ergibt eine neue ID: Browser sehen dann eine neue Erweiterung, Redirect-URI, CORS und die' -ForegroundColor Yellow
        Write-Host 'Intune-Richtlinie brauchen die neue ID.' -ForegroundColor Yellow
    }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
