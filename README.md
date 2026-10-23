# Sentinel Seed – Pipeline-Templates

Azure-DevOps-Pipeline-Templates für Projekte aus dem [Sentinel-Seed-Template](https://github.com/blackforestsentinel/seed-template). Projekte binden sie per `extends` ein und pinnen einen Tag:

```yaml
# Nur von Hand ankreuzen: erlaubt einem Lauf, Daten zu löschen (siehe Schutz vor Datenverlust)
parameters:
  - name: confirmDataDeletion
    displayName: Datenlöschung bestätigen (Storage, Key Vault, Logs)
    type: boolean
    default: false

resources:
  repositories:
    - repository: seed
      type: github
      name: blackforestsentinel/seed-pipelines
      ref: refs/tags/v0.13.0
      endpoint: github-blackforestsentinel

extends:
  template: templates/web-app.yml@seed
  parameters:
    project: kundenportal
    serviceConnection: sc-kundenportal
    terraformState:
      resourceGroup: rg-terraform-state
      storageAccount: stterraformstate
      container: tfstate
    environments:
      - name: dev
      - name: prod
        serviceConnection: sc-kundenportal-prod   # optional, sonst serviceConnection
    confirmDataDeletion: ${{ parameters.confirmDataDeletion }}
    smokeTest:
      protectedPath: api/me                       # mit sso: 401 ohne Token erwartet
    # pool: Default                               # eigener Agent-Pool, siehe Eigene Agents
    # frontend: false                             # Projekt ohne Frontend (nur API)
    # extension:                                  # Browser-Erweiterung als zweiter Client, siehe unten
    #   path: extension
    # preBuildSteps:                              # vor dem Restore, etwa privater Feed, siehe unten
    #   - task: NuGetAuthenticate@1
    # extraBuildSteps:                            # eigene Schritte im Build, siehe unten
    #   - script: npm run lint
    #     displayName: Lint
    #     workingDirectory: frontend
```

## templates/web-app.yml

| Stage | Inhalt |
| --- | --- |
| `build` | Ein Job: Abgleich mit `project.yaml` (Projektname, `hosting.staticWebApp: none` passend zu `frontend: false`), `terraform fmt` und `validate`, die eigenen Schritte aus `preBuildSteps`, `dotnet test` und `dotnet publish` der API samt OpenAPI-Dokument als Artefakt `openapi` (mit `Bfs.Seed.OpenApi`), `npm test` und `npm run build` des Frontends (nur mit Frontend), mit `extension` Build, Tests und Manifest-Prüfung der Browser-Erweiterung, danach die eigenen Schritte aus `extraBuildSteps`; im PR-Lauf zusätzlich der Secret-Scan |
| `plan_<env>` | `terraform plan` gegen den Remote-State `<project>/<env>.tfstate`; merkt sich, ob sich die Infrastruktur ändert. Bei Änderungen liegt der Plan neben dem State (siehe Plan neben dem State), ohne Änderungen kommen die Outputs als Artefakt `tfoutputs_<env>` |
| `apply_<env>` | Nur bei Änderungen: Deployment-Job auf das Environment `<project>-<env>` (dort hängt die Freigabe), `terraform apply` des geprüften Plans, danach die Outputs als Artefakt `tfoutputs_<env>` |
| `deploy_<env>` | Deployment-Job auf das Environment `<project>-<env>-app`, ohne Checkout (außer mit `extension.signing`) und ohne `terraform init`: Outputs aus `tfoutputs_<env>`, `config.json` aus dem Output `frontend_config`, `__API_ORIGIN__` in der CSP ersetzen, Function und Static Web App deployen, Status eigener Domains melden, Smoke-Test, mit `features.customConnector` der Custom Connector, mit `extension` das Paket der Browser-Erweiterung als Artefakt `extension_<env>`, mit `extension.signing` zusätzlich signiert und im Projekt-Storage veröffentlicht (Artefakt `extension_release_<env>`) |

Ohne Infrastruktur-Änderungen entfällt `apply_<env>` samt Freigabe. Ein Plan, der nur Ressourcen im State verschiebt (`moved`-Blöcke nach einem Update von seed-terraform), zählt nicht als Änderung: Terraform wiederholt die Verschiebung bei jedem Plan und speichert sie mit dem nächsten echten Apply. `deploy_<env>` lässt sich nach einem Fehler einzeln neu starten, weil sie keinen Plan anwendet. Scheitert `apply_<env>` an einem veralteten Plan, braucht es einen neuen Lauf.

Umgebungen laufen in der Reihenfolge der Liste nacheinander.

### Plan neben dem State

Eine Plan-Datei enthält den State samt aller sensiblen Werte im Klartext, etwa den Deployment-Token der Static Web App. Als Artefakt am Lauf könnte sie jeder laden, der die Pipeline lesen darf, und mit dem Token eigene Inhalte auf die Web-App deployen. Der Plan liegt deshalb im Container des Terraform-States unter `plans/<project>/<env>/<Build.BuildId>.tfplan`; dort kommt nur hin, wer auch den State lesen darf. `apply_<env>` lädt ihn von dort und löscht ihn nach dem Apply. Pläne, die nie angewendet werden (Freigabe abgelehnt oder abgelaufen), löscht der nächste Plan derselben Umgebung nach 31 Tagen. Die Deployment-Identität braucht dafür keine weiteren Rechte: `Storage Blob Data Contributor` auf dem Container hat sie aus dem Onboarding.

Bis v0.10.0 lag der Plan als Artefakt `tfplan_<env>` am Lauf. Wer von dort umsteigt, setzt nach dem ersten Lauf mit der neuen Version die Deployment-Tokens der Static Web Apps zurück (`az staticwebapp secrets reset-api-key --name <swa> --resource-group <rg>`); der Deploy liest den Token jedes Mal neu. Die alten Artefakte enthalten dann nur noch ungültige Tokens. Die übrigen Schlüssel im State (Storage, Application Insights) sind ohne Wirkung, weil die Seed-Module Shared Key und lokale Anmeldung abschalten.

### Laufzeit

Ein Lauf ohne Infrastruktur-Änderung dauert rund 4 Minuten (vorher 7). Die Organisation hat einen parallelen Job; deshalb gibt es im Build nur einen Job statt dreier, die ohnehin nacheinander liefen. Das .NET SDK des Images genügt, wenn es zu `global.json` passt; sonst installiert `UseDotNet` es. `tfoutputs_<env>` (Outputs ohne sensible Werte, dazu `project.yaml`) erspart dem Deploy das `terraform init`.

Die Function geht über Kudu (`/api/publish`) mit einem Entra-Token der Service Connection raus, nicht über `az functionapp deployment source config-zip`: Das wartet nach dem Deploy fest 60 Sekunden und prüft dann den Host. Kudu verarbeitet das Paket rund eine Minute lang; währenddessen läuft der Deploy der Static Web App, danach wartet die Pipeline auf den Status des Deployments. Ob die neue Fassung läuft, prüft der Smoke-Test über die Build-Nummer. Eine frisch angelegte Function antwortet einige Minuten mit 503, bis die Storage-Rollen ihrer Identität wirken; der Deploy versucht es dann bis zu 10 Minuten lang erneut. Scheitert der Function-Deploy endgültig, ist die Static Web App schon neu; ein erneuter Lauf von `deploy_<env>` gleicht das aus.

Caching (NuGet, npm, Terraform-Provider) fehlt bewusst: Auf den gehosteten Agents ist der NuGet-Cache rund 1 GB groß und der Provider-Cache 670 MB, beides wiederherzustellen dauert länger als `dotnet restore` (7 bis 15 s) und `terraform init` (7 s) ohne Cache. `npm ci` war auch mit Cache-Treffer nicht schneller, und jeder neue Cache-Schlüssel kostet beim Sichern rund eine Minute. Projekte brauchen deshalb auch keine NuGet-Lockfiles.

### PR-Validierung

Läufe mit `Build.Reason` `PullRequest` (ausgelöst von der Branch-Policy auf `main`, die `seed-scaffold` anlegt) bestehen nur aus `build`: Build, Tests, `terraform fmt` und `validate` ohne Backend und ein Secret-Scan. Plan, Apply und Deploy entstehen gar nicht erst, der Lauf braucht also weder die Azure-Service-Connection noch Environments. Es bleibt bei einer Pipeline-Definition je Projekt; eine zweite bräuchte eigene Freigaben für Environments und Service Connection. `pr: none` im Projekt bleibt stehen: In Azure Repos startet die Branch-Policy PR-Läufe, nicht der Trigger.

Mit `validationOnly: true` läuft dieselbe Prüfung ohne PR, etwa um sie von Hand nachzustellen.

Der Secret-Scan nutzt [gitleaks](https://github.com/gitleaks/gitleaks) in fester Version, der Download wird per SHA-256 geprüft. Er prüft alle Commits des PRs (`HEAD^1..HEAD^2` des Merge-Commits), nicht nur den Endstand: Ein Secret, das ein späterer Commit entfernt, bliebe sonst in der Historie. Gefundene Werte erscheinen geschwärzt im Log. Bei falschem Alarm den Fingerprint aus dem Log in `.gitleaksignore` im Projekt eintragen; eigene Regeln gehören in `.gitleaks.toml`. In Läufen auf `main` scannt die Pipeline nicht: Mit der Pflicht-Policy kommt jede Änderung über einen PR.

### Schutz vor Datenverlust

Löscht oder ersetzt ein Plan Ressourcen mit Daten, endet der Lauf in `plan_<env>` mit einer Liste dieser Ressourcen, noch vor jeder Freigabe. Geschützt sind standardmäßig Storage Accounts samt Tabellen, Queues, Containern und Freigaben, Key Vaults, Log-Analytics-Workspaces und Löschsperren (`protectedResourceTypes`). Das trifft etwa eine Tabelle, die aus `project.yaml` verschwindet, `storage: false` oder ein Modul-Update, das einen Storage Account ersetzen würde.

Ist das Löschen gewollt, startet man die Pipeline von Hand und kreuzt „Datenlöschung bestätigen“ an (`confirmDataDeletion`, Laufzeitparameter in `azure-pipelines.yml`). Der Plan nennt die betroffenen Ressourcen dann als Warnung, die Freigabe folgt wie immer. Ein Push kann diese Bestätigung nicht mitbringen. In diesem Lauf bekommt Terraform `TF_VAR_allow_data_deletion=true`: Das Modul `storage` hebt damit seine Löschsperre auf, die sonst auch Tabellen, Queues und Container vor dem Löschen schützt. Der nächste Lauf ohne Bestätigung setzt sie wieder; dafür braucht er eine Freigabe.

### Privater Paket-Feed

`dotnet restore` und `npm ci` laufen ohne Anmeldung. Nennt `nuget.config` oder `.npmrc` einen privaten Feed in Azure Artifacts, scheitert der Restore deshalb mit 401 (`NU1301` bzw. `E401`). `dotnet restore` wiederholt die Anfrage dann ohne Ende; das Template bricht es nach 10 Minuten mit einem Hinweis auf diesen Abschnitt ab, statt den Job bis zu seinem Timeout zu blockieren. Die Anmeldung kommt über den Parameter `preBuildSteps` (Typ `stepList`); diese Schritte laufen vor `dotnet restore` und `npm ci`:

```yaml
extends:
  template: templates/web-app.yml@seed
  parameters:
    # ...
    preBuildSteps:
      - task: NuGetAuthenticate@1
        displayName: Anmeldung am NuGet-Feed
      - task: npmAuthenticate@0             # nur mit privatem npm-Feed
        displayName: Anmeldung am npm-Feed
        inputs:
          workingFile: frontend/.npmrc
```

- **Reihenfolge:** nach Checkout, Abgleich mit `project.yaml`, `terraform validate` und dem .NET SDK laut `global.json`, vor `dotnet restore`. Node in der Version `nodeVersion` richtet das Template erst danach ein; bis dahin gilt die des Images.
- **PR-Lauf:** Die Schritte laufen auch im PR-Lauf und mit `validationOnly`, denn ohne Restore gibt es keinen Build.
- **Rechte:** `NuGetAuthenticate@1` und `npmAuthenticate@0` melden sich ohne Secret mit dem Job-Token an, also als `<Projekt> Build Service (<org>)` oder, wenn das Projekt den Job-Scope nicht auf sich beschränkt, als `Project Collection Build Service (<org>)`. Diese Identität braucht am Feed mindestens die Rolle Feed Reader (Feed settings, Permissions). Liegt der Feed in einem anderen Projekt, braucht sie zusätzlich Lesezugriff auf dieses Projekt (Gruppe Readers). Für Feeds einer anderen Organisation oder außerhalb von Azure DevOps nennt der Schritt eine NuGet-Service-Connection des Projekts (`nuGetServiceConnections`). Eine Azure-Service-Connection bekommen die Schritte nicht.
- Für die Anmeldung taugt `extraBuildSteps` nicht: Diese Schritte laufen erst nach Build und Tests.

### Eigene Schritte im Build

Projektspezifische Prüfungen oder Pakete kommen über den Parameter `extraBuildSteps` (Typ `stepList`) in den Build-Job, statt das Template zu kopieren:

```yaml
extends:
  template: templates/web-app.yml@seed
  parameters:
    # ...
    extraBuildSteps:
      - script: npm run lint
        displayName: Lint Frontend
        workingDirectory: frontend
      - script: |
          npm ci && npm run build
          mkdir -p ../frontend/dist/widgets
          cp dist/widget.js ../frontend/dist/widgets/
        displayName: Widget bauen und ins Frontend legen
        workingDirectory: widget
```

- **Reihenfolge:** nach `dotnet test`/`dotnet publish` und nach `npm test`/`npm run build` des Frontends, vor dem Veröffentlichen des Artefakts `web`. Was ein Schritt nach `<frontendPath>/dist` legt, wird also mit der Static Web App ausgeliefert. Die Abhängigkeiten des Frontends (`node_modules`) sind dann schon installiert, Node und das .NET SDK ebenso.
- **PR-Lauf:** Die Schritte laufen auch im PR-Lauf und mit `validationOnly`; eine Prüfung hier hält also den PR auf.
- **Arbeitsverzeichnis** ist der Checkout, je Schritt änderbar mit `workingDirectory`.
- **Rechte:** Der Build-Job hat keine Azure-Service-Connection und keine Secrets. Schritte, die etwas in Azure brauchen, gehören nicht in den Build.

### Vorschau ohne Lauf

Ob ein Projekt-YAML mit dem Template zusammenpasst, zeigt die Vorschau-API von Azure DevOps (`previewRun`): Sie löst Templates und Ausdrücke auf und gibt das fertige YAML zurück, ohne einen Lauf zu starten, ohne Freigaben und ohne Service Connection. [`tests/preview/Invoke-SeedPipelinePreview.ps1`](tests/preview/Invoke-SeedPipelinePreview.ps1) schickt eine YAML-Datei als `yamlOverride` an eine beliebige vorhandene Pipeline des Azure-DevOps-Projekts, legt das Ergebnis als `<datei>.final.yml` ab und prüft optional die Reihenfolge von Schritten im Build-Job:

```powershell
$common = @{
  Organization = 'https://dev.azure.com/<org>'; Project = '<projekt>'; PipelineId = <id>
  SeedRef = 'refs/heads/<branch>'
  Replace = @{
    GITHUB_CONNECTION = 'github-blackforestsentinel'; SERVICE_CONNECTION = '<sc-im-projekt>'
    PROJECT = '<seed-projekt-im-projekt>'; ENVIRONMENT = '<umgebung>'
  }
}
# Normaler Lauf: Anmeldung am Feed vor dem Restore, eigene Schritte nach dem Frontend-Build und
# vor dem Artefakt web
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extra-build-steps.yml `
  -ExpectBuildSteps '.NET SDK laut global.json', 'Anmeldung am Feed (Beispiel)', 'dotnet restore', `
    'npm run build', 'Lint (Beispiel)', 'Datei ins Frontend (Beispiel)', 'Artefakt web' `
  -AbsentBuildSteps 'npm ci (Erweiterung)', 'Manifest der Erweiterung prüfen', 'Artefakt extension'
# Mit Browser-Erweiterung: ihr Build nach dem Frontend und vor den eigenen Schritten
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extension.yml `
  -ExpectBuildSteps 'npm run build', 'npm ci (Erweiterung)', 'npm test (Erweiterung)', `
    'npm run build (Erweiterung)', 'Manifest der Erweiterung prüfen', 'Lint (Beispiel)', 'Artefakt web', `
    'Artefakt extension'
# Prüflauf wie im PR: eigene Schritte ja, Artefakte nein
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/validation-only.yml `
  -ExpectBuildSteps 'Anmeldung am Feed (Beispiel)', 'dotnet restore', 'npm run build', 'Lint (Beispiel)' `
  -AbsentBuildSteps 'Artefakt web', 'Artefakt api'
# Erweiterung ohne signing: im Deploy nur Paket und Artefakt
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extension.yml `
  -ExpectDeploySteps 'Custom Connector', 'Browser-Erweiterung packen', "Artefakt extension_$($common.Replace.ENVIRONMENT)" `
  -AbsentDeploySteps 'Erweiterung signieren und veröffentlichen'
# Erweiterung mit signing: Checkout, Schlüssel, Veröffentlichen, danach erst die Artefakte
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extension-signing.yml `
  -ExpectBuildSteps 'Manifest der Erweiterung prüfen' `
  -ExpectDeploySteps 'Browser-Erweiterung packen', 'seed-pipelines auschecken (Skript der Erweiterung)', `
    'Signaturschlüssel der Erweiterung', 'Erweiterung signieren und veröffentlichen', `
    "Artefakt extension_$($common.Replace.ENVIRONMENT)", "Artefakt extension_release_$($common.Replace.ENVIRONMENT)"
```

- `-SeedRef` ersetzt die `ref` der Repository-Ressource `seed`, damit sich ein Branch von seed-pipelines vor dem Release prüfen lässt; der Branch muss dafür auf GitHub liegen.
- `-ExpectDeploySteps` und `-AbsentDeploySteps` prüfen die erste Stage `deploy_<umgebung>` wie die Build-Schritte. Mit `-FinalYamlPath <datei>.final.yml` statt Organisation, Projekt und YAML prüft das Skript ein gespeichertes Ergebnis erneut, ohne Azure DevOps.
- Die Pipeline-ID dient nur als Rahmen (Projekt, GitHub-Service-Connection); ihre eigene YAML-Datei spielt keine Rolle. Service Connections und Environments, die das YAML nennt, müssen im Projekt existieren, sonst lehnt Azure DevOps schon die Vorschau ab. Deshalb nennt das Beispiel für den normalen Lauf ein vorhandenes Seed-Projekt und eine seiner Umgebungen; der Prüflauf braucht beides nicht.
- Ebenso lässt sich das YAML eines Projekts vor dem Umstellen prüfen, etwa bei einer Migration, deren Projekt in Azure DevOps noch keine Seed-Service-Connection hat: `-Replace` bzw. eine angepasste Kopie mit einer Service Connection des Rahmenprojekts.

### Smoke-Test

`templates/steps/smoke-test.yml` prüft nach dem Deploy:

- **API:** `/api/health` meldet die Build-Nummer dieses Laufs (`version` aus `SeedHealthReport`, das .NET SDK hängt `+<commit>` an). Bis zu 3 Minuten lang, denn kurz nach dem Deploy antwortet noch die alte Fassung oder ein 503.
- **CORS** (nur mit Frontend): Preflight vom Ursprung der Static Web App ist erlaubt, von einem fremden Ursprung nicht.
- **Token-Pflicht** (nur mit `sso: true` und `smokeTest.protectedPath`): Die angegebene Function ohne `[AllowAnonymous]` antwortet ohne Token mit 401. Das Template trägt dafür `api/me` ein.
- **Frontend:** Startseite und `config.json` erreichbar, `Content-Security-Policy`-Header vorhanden und ohne Platzhalter.

Weitere Prüfungen je Feature kommen als eigener Schritt in `smoke-test.yml`, geschaltet über `condition: eq(variables['feature.<name>'], 'true')`. Der erste Schritt setzt diese Variablen aus `features` in `project.yaml`, das so die einzige Wahrheit bleibt. Projektspezifische Angaben wie Pfade kommen über den Parameter `smokeTest`.

### Custom Connector

Mit `features.customConnector` in `project.yaml` liefert Terraform den Output `custom_connector`, und `templates/steps/custom-connector.yml` legt nach dem Smoke-Test einen Custom Connector der Power Platform an oder aktualisiert ihn:

1. Das OpenAPI-Dokument aus dem Build (Artefakt `openapi`, Paket `Bfs.Seed.OpenApi`) bekommt den Host der Function dieser Umgebung.
2. Der Connector entsteht per Dataverse-Web-API in der Tabelle `connectors` der Power-Platform-Umgebung, wie bei `pac connector create`; ab dem zweiten Lauf wird er aktualisiert. Bestehende Verbindungen bleiben erhalten. Der Name bleibt, der Anzeigename darf sich ändern (höchstens 30 Zeichen).
3. Anmeldung mit Entra ID im Namen der Person, ohne Secret: Der Connector nutzt seine Managed Identity (Option der Power Platform, Preview). Redirect-URI und Federated Credential, die die Power Platform dafür vergibt, trägt der Schritt an der Client-Registrierung aus dem Modul `sso` ein; die Pipeline ist deren Besitzerin.
4. Mit `customConnector.shareWith: everyone` (Standard) bekommt das Standardteam der Root-Business-Unit Lesezugriff, also alle Personen der Umgebung. Ohne Freigabe sähe nur die Pipeline den Connector, denn Basic User liest Connectors nur auf Benutzerebene. Was eine Person über den Connector darf, regeln wie im Browser App-Rollen und Capabilities der API.

Voraussetzung: Die Service-Connection-Identität ist in der Power-Platform-Umgebung Anwendungsbenutzer mit der Rolle Environment Maker; das richtet das Onboarding mit `-PowerPlatformEnvironmentUrl` ein. Fehlt das, endet der Schritt mit einem Hinweis darauf. Ohne Output `custom_connector` (Feature aus oder keine Umgebung angegeben) tut er nichts; das Artefakt `openapi` gibt es trotzdem, im Zweifel mit einer Notiz statt des Dokuments.

### Browser-Erweiterung

Eine Browser-Erweiterung für Edge (Manifest V3) kann zweiter Client derselben API sein, mit oder ohne Frontend. Das ist ein Sonderfall: Ohne den Parameter `extension` ändert sich an der Pipeline nichts.

```yaml
extends:
  template: templates/web-app.yml@seed
  parameters:
    # ...
    extension:
      path: extension                # Ordner mit package.json
      # outputPath: dist             # Build-Ausgabe relativ zu path, dort liegt manifest.json
      # name: my-extension           # Dateiname der Pakete, Standard project
      # storeEnvironments: [prod]    # Pakete für Edge Add-ons, ohne key
      # signing:                     # selbst gehostet für verwaltete Geräte, siehe unten
      #   secureFile: my-extension.pem
      #   id: abcdefghijklmnopabcdefghijklmnop
```

- **Build**, auch im PR-Lauf und mit `validationOnly`: nach dem Frontend-Build und vor `extraBuildSteps` `npm ci`, `npm test` und `npm run build` in `path`, mit Node in `nodeVersion`. Danach muss `<path>/<outputPath>/manifest.json` Manifest V3 sein und eine gültige `version` haben. Im Hauptlauf wird die Build-Ausgabe zum Artefakt `extension`.
- **Deploy:** Als letzter Schritt von `deploy_<env>`, nach Smoke-Test und Custom Connector, entsteht aus einer Kopie das Paket `<name>-<env>.zip` (Artefakt `extension_<env>`, `manifest.json` im Wurzelverzeichnis). Darin `config.json` wie beim Frontend aus dem Output `frontend_config`, aber ohne `auth.redirectBridgePath`, denn die Erweiterung hat keine Bridge-Seite. Anders als sonst gibt es also je Umgebung ein eigenes Paket, weil die Konfiguration im Paket steckt; gebaut und getestet wird trotzdem nur einmal.
- **Version:** die ersten drei Zahlen aus `version` im `manifest.json` des Repos (fehlende als 0, eine vierte wird ersetzt), dazu die Build-ID als vierte, etwa `1.2.0.2160`. So steigt die Version mit jedem Lauf, wie der Store es verlangt; die ersten drei Stellen pflegt das Team im Repo. Edge erlaubt je Stelle höchstens 65535, eine größere Build-ID bricht ab.
- **`key`:** Im Paket für die Umgebungen aus `storeEnvironments` entfernt die Pipeline `key` aus dem Manifest, denn Edge Add-ons vergibt die ID selbst. In den übrigen bleibt `key`, damit die entpackte Erweiterung ihre feste ID und damit ihre Redirect-URI behält; fehlt er dort, warnt die Pipeline.
- **Veröffentlichen von Hand:** Das Paket der Store-Umgebung lädt ein Mensch in Edge Add-ons hoch, die übrigen werden für Tests entpackt geladen. Eine Anbindung an die API von Edge Add-ons gibt es nicht. Ohne Store geht es mit `signing` (unten).
- Redirect-URI und CORS für die Erweiterung setzt das Projekt in `project.yaml` (README von seed-template, „Browser-Erweiterungen“).

#### Signieren und Veröffentlichen für verwaltete Geräte

Statt über Edge Add-ons kann das Projekt die Erweiterung selbst ausliefern: Mit `extension.signing` signiert `deploy_<env>` das Paket als CRX3 und legt es im Daten-Storage des Projekts ab. Die API liefert es mit [`Bfs.Seed.Extension`](https://github.com/blackforestsentinel/seed-packages/tree/main/dotnet/src/Bfs.Seed.Extension) unter `/api/extension/updates.xml` aus; verwaltete Geräte installieren es von dort per Intune-Richtlinie (`ExtensionInstallForcelist`) und holen Updates selbst, alle paar Stunden.

```yaml
extends:
  template: templates/web-app.yml@seed
  parameters:
    # ...
    extension:
      path: extension
      name: my-extension
      signing:
        secureFile: my-extension.pem           # Secure File mit dem privaten Schlüssel (PEM)
        id: abcdefghijklmnopabcdefghijklmnop   # Erweiterungs-ID, die aus dem Schlüssel folgt
        # environments: [prod]                 # Standard [prod], auch ein einzelner Name
```

- **Ablauf** in den Umgebungen aus `signing.environments`, nach dem Paket `<name>-<env>.zip`: [`scripts/extension/publish.py`](scripts/extension/publish.py) setzt im Manifest `update_url` auf `<apiBaseUrl>/api/extension/updates.xml`, packt das Zip deterministisch und signiert es mit dem Schlüssel aus der Secure File. Ergibt der Schlüssel eine andere ID als `signing.id`, bricht der Schritt vor dem Upload ab. Danach lädt er in den Container `extension` des Storage Accounts mit dem Tag `seed:module=storage` (Resource Group der Umgebung) zuerst `<name>-<version>.crx` und dann `current.json` hoch. Zuletzt fragt er `updates.xml` ab, bis die API die neue Version mit dem passenden CRX ausliefert, bis zu drei Minuten lang; jede Instanz hält `current.json` eine Minute im Speicher. CRX und `release.json` (ID, Version, Datei, SHA-256, `updateUrl`) kommen als Artefakt `extension_release_<env>`.
- **Unveränderter Inhalt:** Ist der Inhalt ohne die Version derselbe wie der veröffentlichte, lädt der Schritt nichts hoch und prüft nur die Auslieferung. Sonst bekämen die Geräte mit jedem Lauf ein Update, weil die Version mit der Build-ID steigt.
- **Downgrade-Schutz:** Eine kleinere oder gleiche Version mit anderem Inhalt bricht ab, etwa wenn `deploy_<env>` eines älteren Laufs neu gestartet wird. Browser installieren keine kleinere Version, und ein altes Paket unter derselben Version wäre ein anderes als das, was Browser schon haben. Einen alten Stand bringt ein neuer Lauf mit zurückgenommener Änderung.
- **`key`:** Das Paket einer signierten Umgebung hat wie die Store-Pakete kein `key`; die ID folgt aus dem Signaturschlüssel. In den übrigen Umgebungen bleibt `key` für die entpackte Erweiterung.
- **Rechte:** Der Schritt läuft mit der Service Connection der Umgebung und schreibt mit `az storage blob … --auth-mode login`. Die Deployment-Identität braucht dafür `Storage Blob Data Contributor` auf dem Container `extension`; das vergibt das Modul `storage` aus seed-terraform mit `extension_publisher_principal_id`, seed-template übergibt die Identität, mit der Terraform läuft. Eine neue Rolle wirkt erst nach einigen Minuten; bis dahin wiederholt der Schritt den Zugriff bis zu 10 Minuten lang.
- **Im Projekt** (README von seed-template, „Browser-Erweiterungen“): `features.storage` mit dem Container `extension` in `storage.containers`, das Paket `Bfs.Seed.Extension` mit `builder.AddSeedExtensionUpdates()` in `Program.cs`, Redirect-URI und CORS mit der ID aus dem Schlüssel. Weicht `extension.name` vom Projektnamen ab, braucht die API das App-Setting `Extension__PackageName`. Fehlt der Container, nennt der Schritt ihn; liefert `updates.xml` nichts, fragt er nach `AddSeedExtensionUpdates()`.
- **Checkout im Deploy:** Für das Skript checkt `deploy_<env>` seed-pipelines selbst aus (`checkout: seed` nach `$(Pipeline.Workspace)/seed`, dieselbe `ref` wie die Templates), wie `intune-extensions.yml`; bisher kam der Deploy ohne Checkout aus. So steht die Logik nicht als Inline-Python im Template, sondern ist mit Tests abgedeckt (`python3 -m unittest discover -s scripts -p "test_*.py"`, braucht openssl; läuft in der GitHub-CI dieses Repos). Die Repository-Ressource muss dafür `seed` heißen, wie in allen Beispielen.
- **Neustart:** Die Artefakte entstehen erst nach dem Veröffentlichen. Scheitert ein Schritt davor, kommt `deploy_<env>` mit einem Neustart durch; was schon hochgeladen ist, erkennt der Schritt am Inhalt.
- **Intune:** Die Richtlinie (`ExtensionInstallForcelist` mit `<id>;<update_url>`) pflegt die zentrale Pipeline `seed-intune` je Tenant (siehe [Intune: Edge-Erweiterungen verteilen](#intune-edge-erweiterungen-verteilen)). Nach dem ersten erfolgreichen Veröffentlichen kommt die Erweiterung per Pull Request in deren Registerdatei; `id` und `updateUrl` stehen in `release.json` (Artefakt `extension_release_<env>`). Vorher lehnt die Prüfung dort den Eintrag ab, weil `updates.xml` die ID noch nicht ausliefert. Edge installiert Erweiterungen außerhalb des Stores nur auf verwalteten Geräten.

#### Schlüssel der Erweiterung

Die Erweiterungs-ID folgt aus dem öffentlichen Schlüssel (SHA-256, die ersten 16 Byte als `a` bis `p`). [`onboarding/New-SeedExtensionKey.ps1`](onboarding/New-SeedExtensionKey.ps1) erzeugt lokal einen RSA-Schlüssel (2048 Bit, PEM, PKCS#8), zeigt die ID, lädt den Schlüssel als Secure File ins Azure-DevOps-Projekt und berechtigt die Pipeline dafür. Es läuft mit dem `az`-Login einer Person, die im Projekt Secure Files anlegen darf; `-DryRun` zeigt nur an, was fehlt.

```powershell
./onboarding/New-SeedExtensionKey.ps1 -KeyFile C:\keys\my-extension.pem `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -PipelineName <pipeline> -DryRun
```

- **Ausgabe:** die Werte für `extension.signing` und für `project.yaml` je signierter Umgebung, `auth.extraRedirectUris` (`https://<id>.chromiumapp.org/`) und `hosting.corsAllowedOrigins` (`chrome-extension://<id>`).
- **Den Schlüssel sicher offline ablegen** (Passwortmanager, Tresor) und die Datei danach löschen. Azure DevOps gibt Secure Files nicht wieder heraus, und ein neuer Schlüssel ergibt eine neue ID: Browser sehen eine neue Erweiterung, Redirect-URI, CORS und die Intune-Richtlinie brauchen die neue ID.
- **Wiederholen:** Gibt es die Secure File schon, entsteht kein neuer Schlüssel. Mit `-ExistingKey -KeyFile <pem>` lädt das Skript einen vorhandenen Schlüssel hoch, falls die Secure File fehlt, und prüft ihn sonst gegen die ID, die es beim Hochladen an der Secure File vermerkt hat (Eigenschaft `extensionId`).
- Der private Schlüssel erscheint in keiner Ausgabe. Im Deploy liegt er nur für die Dauer des Jobs auf dem Agent (`DownloadSecureFile@1`).

### Voraussetzungen

- GitHub-Service-Connection in der Azure-DevOps-Organisation (Name frei wählbar, im Projekt als `endpoint` angegeben).
- Azure-Service-Connection mit Workload Identity Federation. Terraform bekommt deren OIDC-Token, es gibt keine Secrets.
- Rollen der Service-Connection-Identität: `Contributor` und `Role Based Access Control Administrator` auf der Subscription, `Storage Blob Data Contributor` auf dem State-Storage.
- Je Umgebung zwei Environments, für die Pipeline berechtigt: `<project>-<env>` mit Freigabe als Check (Infrastruktur) und `<project>-<env>-app` für den App-Deploy, dort optional eine Freigabe, etwa in Produktion. Azure DevOps legt sie nicht selbst an.

### Eigene Agents

Mit `pool: <Name>` laufen alle Jobs im angegebenen Agent-Pool statt auf Microsoft-gehosteten Agents (`vmImage`). Das gilt ebenso für `templates/scaffold.yml` und `templates/intune-extensions.yml`. Gedacht ist das für Organisationen ohne gehostete Minuten oder für Agents im eigenen Netz.

- **Was die Agents brauchen:** Linux (x64) mit bash, curl, unzip, tar, git, python3 und der Azure CLI. Für `features.customConnector` kommt PowerShell 7 (`pwsh`) dazu, für `extension.signing` openssl.
- **Was die Pipeline selbst lädt:** das .NET SDK laut `global.json`, Node in `nodeVersion`, Terraform und gitleaks, jeweils ins Tool-Verzeichnis des Agents.
- **Anmeldung:** Die Azure CLI meldet sich in jedem Schritt mit einem eigenen Konfigurationsverzeichnis an (`AZURE_CONFIG_DIR`); eine Anmeldung des Agent-Benutzers bleibt unberührt.
- **Reihenfolge:** Mit einem einzigen Agent laufen Stages und Läufe nacheinander, wie mit einem gehosteten parallelen Job.
- **Berechtigung:** Der Pool muss im Projekt für die Pipeline berechtigt sein, sonst fragt der erste Lauf danach („Permit“).

### Projektstruktur, die das Template erwartet

`project.yaml`, `api/Api.slnx`, `api/Api/Api.csproj`, `frontend/` mit `npm test` und `npm run build` nach `dist/` (nur mit Frontend), `infra/` mit den Outputs `resource_group_name`, `function_app_name`, `function_app_url`, `static_web_app_name`, `static_web_app_url` (ohne Frontend leere Strings) und optional `frontend_config` (Objekt, wird zu `config.json`). Pfade sind per Parameter änderbar.

### Security-Header des Frontends

Die Header setzt das Projekt in `frontend/public/staticwebapp.config.json` (`globalHeaders`, Vorlage in seed-template). Die Content-Security-Policy enthält in `connect-src` den Platzhalter `__API_ORIGIN__`; `deploy_<env>` ersetzt ihn im selben Schritt, der `config.json` schreibt, durch den Ursprung der Function-URL (`https://func-….azurewebsites.net`). Ohne Ersatz ist der Platzhalter keine gültige Quelle, die CSP blockiert dann die API, statt offen zu sein.

Der Smoke-Test prüft, dass das Frontend einen `Content-Security-Policy`-Header liefert und `__API_ORIGIN__` darin nicht mehr vorkommt. Projekte, die von v0.3.0 kommen, übernehmen dafür `staticwebapp.config.json` aus seed-template, sonst scheitert der Smoke-Test.

### Projekt ohne Frontend

Mit `frontend: false` (passend zu `hosting.staticWebApp: none` in `project.yaml`) entfallen der Frontend-Build, `config.json`, das Deployment-Token und der Deploy der Static Web App; der Smoke-Test prüft nur die API. Der Ordner `frontend/` wird nicht gebraucht und darf fehlen. Passen Flag und `project.yaml` nicht zusammen, bricht `build` mit einer Fehlermeldung ab.

### Eigene Domains

Hat die Static Web App eigene Domains (`hosting.customDomains`, Modul `core` aus seed-terraform), meldet `deploy_<env>` jede Domain, die Azure noch nicht validiert hat, als Warnung mit den nötigen DNS-Einträgen (TXT `_dnsauth.<domain>` und CNAME). Ist die Domain bereit, verschwindet die Warnung.

## Tenant-Onboarding

[`onboarding/Initialize-SeedTenant.ps1`](onboarding/Initialize-SeedTenant.ps1) richtet einmal je Subscription und Azure-DevOps-Projekt alles ein, was Seed-Projekte brauchen. Es läuft mit dem `az`-Login einer Person, die Owner der Subscription, Global Administrator (oder Privileged Role Administrator) und Projektadministrator in Azure DevOps ist. Jeder Schritt prüft zuerst und legt nur an, was fehlt; mit `-DryRun` zeigt das Skript nur an, was fehlt.

```powershell
git clone --branch v0.13.0 https://github.com/blackforestsentinel/seed-pipelines.git
cd seed-pipelines
./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -Approvers it@kunde.de -DryRun
```

1. Deployment-Identität: App-Registrierung ohne Secret
2. Azure: `Contributor` und `Role Based Access Control Administrator` auf der Subscription. Die Bedingung erlaubt nur die Rollen, die Seed-Module vergeben: Storage-Datenrollen, `Key Vault Secrets User` und `Key Vault Secrets Officer`, `Monitoring Metrics Publisher`. Nach einem Seed-Update mit neuen Rollen das Skript erneut ausführen; es ersetzt dann die ältere Bedingung. Dazu kommt die eigene Rolle `Sentinel Seed Lock Contributor`, die nur Löschsperren lesen, setzen und entfernen darf (Modul `storage`); Contributor allein darf das nicht.
3. Microsoft Graph: `Application.ReadWrite.OwnedBy` mit Admin-Consent (Modul `sso`)
4. Terraform-State: Storage Account ohne Shared Key, `Storage Blob Data Contributor` nur auf dem Container
5. Service Connection per Workload Identity Federation, mit Branch-Prüfung (siehe unten)
6. GitHub-Service-Connection (Token aus `$env:SEED_GITHUB_TOKEN`, sonst Hinweis)
7. Repo und Pipeline `seed-scaffold` (entfällt mit `-SkipScaffold`)
8. GitHub-Service-Connection für alle Pipelines
9. Nur mit `-PowerPlatformEnvironmentUrl <url>[,<url>]`: die Deployment-Identität als Anwendungsbenutzer mit der Rolle Environment Maker in jeder genannten Power-Platform-Umgebung (Umgebungs-URL aus dem Power Platform Admin Center, die Umgebung braucht Dataverse), für den Custom Connector. Das Konto braucht dort System Administrator. Weitere Umgebungen später: Skript erneut mit allen URLs ausführen.

**Weitere Subscription im selben Tenant:** Das Skript erneut ausführen, mit eigener Identität und Service Connection (`-IdentityName`, `-ServiceConnectionName`). Der State-Account bekommt nach Default je Subscription einen eigenen Namen. Die Rolle `Sentinel Seed Lock Contributor` gibt es im Tenant nur einmal: Das Skript sucht sie in allen Subscriptions, die das Konto sieht, und nimmt die neue Subscription in ihre `assignableScopes` auf (im DryRun `[alt]`). Dafür braucht das Konto auch in den Subscriptions, in denen die Rolle schon zuweisbar ist, Owner oder User Access Administrator. Bestehende Identitäten fasst das Skript nicht an.

**Branch-Prüfung:** An der Azure-Service-Connection hängt der Check „Branch control“: Nur Läufe von `-AllowedBranches` (Standard `main`, Muster wie `release/*` erlaubt) bekommen die Deployment-Identität, PR-Läufe und Feature-Branches nicht. Freigaben allein reichen dafür nicht, denn sie hängen an den Environments: Eine geänderte Pipeline-Datei auf einem Feature-Branch, die die Service Connection direkt nutzt, käme ohne die Prüfung an der Freigabe vorbei und hätte die Rechte der Identität auf der ganzen Subscription. Ein erneuter Lauf mit anderen Branches aktualisiert die Prüfung.

Die Prüfung vergleicht **jedes** Repo des Laufs mit derselben Liste, auch `seed-pipelines`, das Projekte über einen Tag einbinden. Deshalb erlaubt sie zusätzlich `-TemplateRefs` (Standard `refs/tags/v*`); ohne sie scheitert jeder Lauf auf `main`. Die Kehrseite: Ein Tag `v…` im Projekt-Repo und ein Lauf darauf kämen ebenfalls durch. In den Projekt-Repos sollten deshalb nur Administratoren Tags anlegen dürfen: unter Project Settings → Repositories → Security „Create tag“ auf Deny für Contributors, Build Administrators, `<Projekt> Build Service` und `Project Collection Build Service`. Seed-Pipelines legen selbst keine Tags an. Wer Seed-Änderungen in einem Testprojekt vor dem Release prüft, nimmt die Branches von seed-pipelines in `-TemplateRefs` auf, etwa `refs/heads/feat/*`.

Mit `-SkipScaffold` setzt das Skript sie nicht, weil eine Migration vor dem Umschalten auf einem eigenen Branch testet; das übernimmt `Initialize-SeedProject.ps1 -EnableBranchPolicy`. Für bestehende Installationen genügt es, `Initialize-SeedProject.ps1` mit `-EnableBranchPolicy` bzw. `Initialize-SeedTenant.ps1` erneut auszuführen.

`-SkipScaffold` lässt Repo und Pipeline `seed-scaffold` weg, für Projekte, die migriert statt neu angelegt werden (siehe [Bestehendes Projekt migrieren](#bestehendes-projekt-migrieren)); der PAT entfällt dann.

`-Pool <Name>` lässt `seed-scaffold` auf eigenen Agents laufen (siehe [Eigene Agents](#eigene-agents)); ohne den Parameter nutzt es Microsoft-gehostete Agents.

Danach einmalig einen PAT anlegen und als geheime Variable `SeedScaffoldPat` an der Pipeline `seed-scaffold` hinterlegen. Scopes: Code (Read, write & manage), Build (Read & execute), Environment (Read & manage), Graph (Read), Project and Team (Read), Pipeline Resources (Use and manage). Das Job-Token der Pipeline reicht nicht, solange „Protect access to repositories in YAML pipelines“ an ist; dann darf es keine Repos anlegen. Die Person, deren PAT es ist, braucht außerdem „Edit policies“ an den Repos des Projekts (Branch-Policy der PR-Validierung).

## templates/scaffold.yml: neues Projekt anlegen

Die Pipeline `seed-scaffold` (Vorlage: [`scaffold/azure-pipelines.yml`](scaffold/azure-pipelines.yml)) fragt beim Start Name, `sso`, `mcp`, `storage`, Frontend, Umgebungen und Freigebende ab und legt über das Terraform-Modul `seed-terraform//ado-project` an:

- Repo aus `seed-template` mit `project.yaml` und `azure-pipelines.yml` (ohne Frontend: `hosting.staticWebApp: none` und `frontend: false`)
- Environments `<name>-<env>` (Freigabe für Infrastruktur) und `<name>-<env>-app`
- Pipeline, berechtigt für die Environments; auf Wunsch startet sie den ersten Lauf
- Branch-Policy auf `main`: Pull Requests brauchen einen erfolgreichen PR-Lauf dieser Pipeline (siehe PR-Validierung). Die Policy ist Pflicht und sperrt damit direkte Pushes auf `main`.

Die Azure-Service-Connection berechtigt `seed-scaffold` bewusst nicht selbst: Im ersten Lauf eines neuen Projekts gibt eine Administratorin oder ein Administrator sie einmal frei („Permit“). PR-Läufe brauchen keine Freigabe, weil sie die Service Connection nicht verwenden. Der State liegt unter `scaffold/<name>.tfstate`; ein erneuter Lauf ergänzt nur, was fehlt.

Für die Branch-Policy reicht der Scope Code (Read, write & manage) des PATs; die Person, deren PAT es ist, braucht am Repo die Berechtigung „Edit policies“ (Projektadministratoren haben sie). Die Policy setzt voraus, dass das neue Projekt seed-pipelines ab v0.5.0 einbindet (`pipelinesVersion`); ältere Versionen würden in PR-Läufen deployen, `ado-project` bricht dann ab.

## Bestehendes Projekt migrieren

Ein Projekt mit eigenem Repo und eigenen Pipelines zieht ohne neues Repo auf Seed um. [`onboarding/Initialize-SeedProject.ps1`](onboarding/Initialize-SeedProject.ps1) legt dafür an, was sonst `seed-terraform//ado-project` für neue Projekte anlegt, mit denselben Einstellungen, aber ohne Repo-Import und ohne Dateien zu schreiben:

- Environments `<name>-<env>` mit Freigabe und `<name>-<env>-app`, dort mit Freigabe nur für `-AppApprovalEnvironments`
- die Pipeline-Definition für die YAML-Datei (Default `azure-pipelines.yml`), ohne sie zu starten
- die Berechtigungen der Pipeline für die Environments, die Azure- und die GitHub-Service-Connection; das „Permit“ beim ersten Lauf entfällt
- mit `-EnableBranchPolicy` die Branch-Policy der PR-Validierung und die Branch-Prüfung an der Azure-Service-Connection (`-AllowedBranches`, Standard der Default-Branch)

Es läuft mit dem `az`-Login einer Person, die Projektadministrator ist. Wie das Onboarding prüft es jeden Schritt zuerst, und `-DryRun` zeigt nur an, was fehlt. Die Reihenfolge:

1. **Onboarding mit `-SkipScaffold`** für die Subscription des Projekts: Identität, Rechte, State und Service Connections.

   ```powershell
   ./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> `
     -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
     -IdentityName sentinel-seed-<kurzname> -ServiceConnectionName sc-<name> -SkipScaffold -DryRun
   ```

2. **Projekt einrichten:**

   ```powershell
   ./onboarding/Initialize-SeedProject.ps1 -AzureDevOpsOrganization https://dev.azure.com/<org> `
     -AzureDevOpsProject <projekt> -Repository <repo> -SeedProjectName <name> `
     -Environments test,prod -Approvers it@kunde.de -AppApprovalEnvironments prod `
     -ServiceConnectionName sc-<name> -DryRun
   ```

3. **Repo umstellen** auf einem eigenen Branch: `project.yaml`, `azure-pipelines.yml` mit `extends` auf `web-app.yml` (siehe oben) und die Projektstruktur, die das Template erwartet.
4. **Pipeline auf dem Branch** von Hand starten (`az pipelines run --name <pipeline> --branch <branch>`) und Plan, Deploy und Smoke-Test prüfen. Ein Lauf auf einem Branch deployt wie einer auf `main`, mit denselben Freigaben; wer zuerst nur die erste Umgebung prüfen will, lehnt die Freigabe der nächsten ab.
5. **Alte Pipelines deaktivieren**, bevor der Branch nach `main` geht (Pipeline-Einstellungen, „Disabled“). Sonst startet der Merge sie mit: eine alte Pipeline auf derselben YAML-Datei mit dem neuen Inhalt, andere mit ihren alten Dateien, solange es diese noch gibt.
6. **Mergen.** Ab dann deployt die Seed-Pipeline bei jedem Push auf `main`.
7. **Branch-Policy nach dem Umschalten:** `Initialize-SeedProject.ps1` mit denselben Parametern und `-EnableBranchPolicy`. Das Skript prüft vorher, dass die YAML-Datei auf `main` `web-app.yml` ab v0.5.0 einbindet; mit der alten Datei würden PR-Läufe deployen. Danach gehen Änderungen an `main` nur noch per PR, und nur Läufe von `main` bekommen die Azure-Service-Connection.

Zwischen Schritt 2 und 6 liegt auf `main` noch die alte Datei. Hat sie denselben Pfad, startet ein Push auf `main` auch die neue Pipeline, mit dem alten Inhalt. Diese Pipeline ist für die alten Service Connections und Environments nicht berechtigt und fragt nach Zugriff: solche Läufe abbrechen, den Zugriff nicht erlauben. `Initialize-SeedProject.ps1` weist darauf hin und listet die übrigen aktiven Pipelines des Repos.

## Intune: Edge-Erweiterungen verteilen

Auf verwalteten Windows-Geräten installiert Edge Erweiterungen außerhalb des Stores über die Richtlinie `ExtensionInstallForcelist` mit Einträgen `<id>;<update_url>`. Intune erlaubt diese Einstellung nur in **einer** Richtlinie: Setzen zwei Richtlinien sie, meldet Intune einen Konflikt, und auf den betroffenen Geräten wirkt keine. Deshalb gibt es je Tenant genau eine Pipeline, die genau eine Richtlinie besitzt: `seed-intune` im Repo `seed-tenant` des Azure-DevOps-Projekts, mit [`templates/intune-extensions.yml`](templates/intune-extensions.yml). Welche Erweiterungen dazugehören, steht in der Registerdatei `extensions.yaml` dieses Repos; Änderungen kommen per Pull Request, angewendet wird erst nach Freigabe. Signieren und Veröffentlichen der Erweiterung bleibt Sache des Projekts (Deploy mit `extension.signing`, siehe [Signieren und Veröffentlichen für verwaltete Geräte](#signieren-und-veröffentlichen-für-verwaltete-geräte)); die Registerdatei nennt nur, wo sie liegt.

```yaml
# seed-tenant/azure-pipelines.yml (Startinhalt aus tenant/, angelegt vom Onboarding)
trigger:
  branches:
    include: [main]
  paths:
    include: [extensions.yaml, azure-pipelines.yml]
pr: none

parameters:
  - name: allowEmpty
    displayName: Alle Erweiterungen entfernen (leere Registerdatei anwenden)
    type: boolean
    default: false

resources:
  repositories:
    - repository: seed                    # Name fest: das Template checkt das Repo aus
      type: github
      name: blackforestsentinel/seed-pipelines
      ref: refs/tags/v0.13.0
      endpoint: github-blackforestsentinel

extends:
  template: templates/intune-extensions.yml@seed
  parameters:
    serviceConnection: sc-seed-intune
    environment: seed-intune
    registry: extensions.yaml
    policyName: 'Sentinel Seed: Edge-Erweiterungen'
    adoptPolicies: ['Sentinel Link: Edge-Erweiterung']
    allowEmpty: ${{ parameters.allowEmpty }}
```

| Stage | Inhalt |
| --- | --- |
| `plan` | Checkout von `seed-tenant` und `seed-pipelines` (derselbe Tag wie das Template). „Registerdatei prüfen“ (`scripts/intune/intune.py check`): Format und je Erweiterung `updates.xml`, ohne Anmeldung. Danach mit der Service Connection `intune.py plan`: Soll-Liste, Änderungen an der Richtlinie, Konfliktprüfung; setzt die Ausgabevariablen `changes` und `planId` |
| `apply` | Nur bei Änderungen: Deployment-Job im Environment `seed-intune` (dort hängt die Freigabe), `intune.py apply` mit der Kennung des freigegebenen Plans, danach liest es die Richtlinie erneut und prüft sie |

Die Pipeline läuft nach jedem Merge auf `main`, der `extensions.yaml` oder `azure-pipelines.yml` ändert (`trigger` mit `paths` im YAML des Tenants; ein Template darf keine Trigger setzen), und von Hand. PR-Läufe (Branch-Policy) und `validationOnly: true` bestehen nur aus „Registerdatei prüfen“: kein Plan, keine Service Connection, kein Environment.

Neu gegenüber den übrigen Templates: Es checkt `seed-pipelines` selbst aus und ruft ein Skript auf, statt Python inline ins YAML zu schreiben. Graph, Übernahme, Konflikte und die Prüfung nach dem Anwenden wären als Inline-Block nicht testbar; so laufen Unit-Tests im CI dieses Repos (`python3 -m unittest discover -s scripts -p "test_*.py"`, nur Standardbibliothek), und Skript und Template kommen immer aus demselben Tag.

### Registerdatei

```yaml
extensions:
  - name: kundenportal                    # Projekt, wie in project.yaml
    id: abcdefghijklmnopabcdefghijklmnop  # 32 Zeichen a bis p, folgt aus dem Signaturschlüssel
    updateUrl: https://func-kundenportal-prod-1a2b3c.azurewebsites.net/api/extension/updates.xml
    owner: it@kunde.de                    # optional
    comment: Seitenleiste für den Support # optional
```

- **Format:** ein bewusst kleiner Ausschnitt aus YAML, den `intune.py` selbst liest: nur der Schlüssel `extensions`, eine Liste von Einträgen mit genau diesen fünf Schlüsseln, Kommentare, Werte ohne, in `'…'` oder in `"…"` Anführungszeichen; `extensions: []` für keinen Eintrag. Abgelehnt mit Zeilennummer werden Tabulatoren, mehrzeilige Werte, `{…}`, Anker, unbekannte oder doppelte Schlüssel und Werte, die YAML nicht als Text läse (`yes`, `2026-01-01`, Zahlen). Jede gültige Datei liest ein YAML-Parser genauso (die Tests prüfen das gegen PyYAML). PyYAML selbst kommt nicht in die Pipeline, weil es auf dem Agent nicht sicher vorhanden ist und ein `pip install` eine weitere Abhängigkeit aus dem Netz wäre. JSON ginge ohne eigenen Parser, kennt aber keine Kommentare; die Datei pflegen Menschen per Pull Request, und Seed nutzt YAML auch für `project.yaml`.
- **Werte:** `name` 3 bis 40 Zeichen (`a-z`, `0-9`, `-`), `id` 32 Zeichen `a` bis `p`, beide eindeutig. `updateUrl` per `https`, ohne Anmeldedaten, Query, Fragment und `;`, endet auf `/api/extension/updates.xml`.
- **Veröffentlicht?** „Registerdatei prüfen“ lädt jede `updateUrl` (bis zu drei Versuche) und verlangt ein Update-Manifest (`gupdate`, Namespace `http://www.google.com/update2/response`) mit `app/@appid` gleich `id` und einem `updatecheck` mit `version` und `https`-`codebase`. Sonst endet der Lauf mit „Erst veröffentlichen (Deploy des Projekts), dann eintragen“, im Pull Request also vor dem Merge.

### Plan und Apply

- **Soll:** alle Einträge der Registerdatei als `<id>;<updateUrl>`, sortiert. Die Pipeline besitzt die ganze Richtlinie: Einträge, die nicht in der Datei stehen, und andere Einstellungen in der Richtlinie entfernt sie; zugewiesen ist sie genau „alle Benutzer“. Für jede entfernte Erweiterung warnt der Plan, denn Edge deinstalliert sie auf allen Geräten.
- **Leere Registerdatei:** Der Plan bricht ab, statt die Richtlinie zu leeren, denn das würde alle Erweiterungen deinstallieren. Ist genau das gewollt, die Pipeline von Hand mit „Alle Erweiterungen entfernen“ (`allowEmpty`, `--allow-empty`) starten; dann löscht sie die Richtlinie.
- **Konflikte:** Setzt eine andere Richtlinie des Einstellungskatalogs ebenfalls `ExtensionInstallForcelist` und ist sie zugewiesen, endet der Plan mit einem Fehler, der sie nennt. Entschieden als Fehler, nicht als Warnung: Solange der Konflikt besteht, wirkt die Richtlinie der Pipeline auf den betroffenen Geräten nicht, und Anwenden änderte daran nichts; ein grüner Lauf würde etwas anderes behaupten. Ob sich die Zuweisungen tatsächlich überschneiden (Gruppen), prüft die Pipeline nicht, im Zweifel hält sie an. Eine Richtlinie ohne Zuweisung (oder nur mit Ausschlüssen) wirkt nirgends und ist nur eine Warnung. Abhilfe: deren Einträge in die Registerdatei übernehmen und die Richtlinie löschen.
- **Freigegebener Plan:** `plan` gibt dem Plan eine Kennung (`planId`, Hash der Schritte). `apply` rechnet den Plan neu und bricht ohne Änderung ab, wenn die Kennung nicht mehr stimmt, etwa weil jemand die Richtlinie in Intune geändert hat oder ein zweiter Lauf schneller war. Dann einen neuen Lauf starten.
- **Nach dem Apply** liest das Skript die Richtlinie erneut (bis zu drei Versuche) und endet mit Fehler, wenn noch etwas fehlt.
- **Technik** wie bei Sentinel Link: Graph beta, Einstellungskatalog (`windows10`, `mdm`), Setting-ID aus einer Liste bekannter Kandidaten (`SETTING_CANDIDATES`), die abhängige Liste aus der Definition; Wiederholungen bei 429 und 5xx. Den Graph-Token holt `az account get-access-token --resource-type ms-graph` im Kontext der Service Connection.

### Übernahme einer bestehenden Richtlinie

Gibt es die Richtlinie `policyName` noch nicht, aber genau eine aus `adoptPolicies`, benennt `apply` diese um und gleicht sie an (Plan: „übernehmen und umbenennen“). Sie wird nie gelöscht und neu angelegt: Dazwischen würde Edge die Erweiterungen deinstallieren und danach neu installieren; lokale Daten der Erweiterungen gingen dabei verloren. Passen mehrere, bricht der Plan mit ihrer Liste ab; übernommen wird nur genau eine, sonst ginge eine Liste verloren. Gibt es `policyName` schon, bleibt eine alte Richtlinie mit derselben Einstellung ein Konflikt (siehe oben).

Für Sentinel Link („Sentinel Link: Edge-Erweiterung“, bisher von `rollout.yml` in Sentinel-Link gepflegt):

1. Onboarding mit `-AdoptPolicies 'Sentinel Link: Edge-Erweiterung'` (landet in `adoptPolicies`).
2. In Sentinel-Link die Stage `intune` aus `rollout.yml` entfernen (oder die Pipeline `sentinel-link-rollout` anhalten), **bevor** `seed-intune` zum ersten Mal anwendet. Sonst findet `rollout/intune.py` seine Richtlinie nach der Umbenennung nicht mehr, legt sie neu an, und beide stehen im Konflikt.
3. Pull Request in `seed-tenant` mit dem Eintrag von Sentinel Link (`id` und `updateUrl` wie bisher). Nach dem Merge zeigt der Plan „übernehmen und umbenennen“, mit derselben Liste ohne Entfernungen.
4. Nach dem Apply der Identität `sentinel-link-rollout` das Recht `DeviceManagementConfiguration.ReadWrite.All` entziehen; das Onboarding nennt alle weiteren Identitäten mit diesem Recht.

### Rechte und Schutz

- **Identität** `sentinel-seed-intune` (App-Registrierung ohne Secret, Workload Identity Federation): Microsoft Graph `DeviceManagementConfiguration.ReadWrite.All` als Anwendungsberechtigung mit Administratorzustimmung. Das Recht gilt für alle Konfigurationsrichtlinien des Tenants; die Pipeline fasst nur ihre eine an, die Checks unten sorgen dafür, dass nur sie die Identität bekommt. Sie ist die einzige Identität mit Intune-Schreibrecht; Projekte und die Deployment-Identitäten bekommen keins.
- **Azure:** nur `Reader` auf der leeren Resource Group `rg-seed-intune`. Eine AzureRM-Service-Connection verlangt eine Subscription, und `AzureCLI@2` wählt sie nach der Anmeldung aus; das scheitert, wenn die Identität dort gar kein Recht hat. Reader auf einer leeren Resource Group macht die Subscription sichtbar und gibt auf nichts Zugriff. (Ein eigener Token-Tausch über die OIDC-Schnittstelle der Pipeline käme ohne Subscription aus, wäre aber ein Sonderweg neben `AzureCLI@2`, dessen Zusammenspiel mit den Checks der Service Connection nicht erprobt ist.) Die Resource Group nicht löschen, sonst scheitert die Anmeldung.
- **Service Connection** `sc-seed-intune` mit zwei Checks: „Branch control“ (`refs/heads/main,refs/tags/v*`, wie bei den Projekten) und „Required template“ (`templates/intune-extensions.yml` aus `blackforestsentinel/seed-pipelines` auf dem verwendeten Tag). Eine geänderte Pipeline-Datei auf einem Branch oder ein anderes Template kommt damit nicht an die Identität. Service Connection und Environment sind nur für `seed-intune` freigegeben, nicht für alle Pipelines.
- **Zwei Freigaben:** Branch-Policies auf `main` von `seed-tenant`: Build-Validierung mit `seed-intune` und mindestens eine Freigabe durch eine andere Person als die, die den Pull Request erstellt oder zuletzt gepusht hat (Stimmen werden bei neuen Pushes zurückgesetzt). Der Review prüft den Inhalt (welche Erweiterung, welche Adresse), die Freigabe im Environment danach den konkreten Plan gegen Intune. Wer „Bypass policies“ am Repo hat, käme am Review vorbei; das Recht nur Administratoren lassen.

### Einrichtung

[`onboarding/Initialize-SeedIntune.ps1`](onboarding/Initialize-SeedIntune.ps1), einmal je Tenant, nach `Initialize-SeedTenant.ps1` (GitHub-Service-Connection). Wie die übrigen Onboarding-Skripte prüft es jeden Schritt zuerst und legt nur an, was fehlt; `-DryRun` ändert nichts, auch nicht in Entra ID oder Azure.

```powershell
./onboarding/Initialize-SeedIntune.ps1 -SubscriptionId <id> `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject 'Sentinel Seed' `
  -Approvers it@kunde.de -AdoptPolicies 'Alte Edge-Richtlinie' -DryRun
```

1. Identität `sentinel-seed-intune` mit Service Principal
2. Graph `DeviceManagementConfiguration.ReadWrite.All` mit Administratorzustimmung; nennt weitere Identitäten mit diesem Recht
3. Resource Group `rg-seed-intune` (leer) und Reader darauf
4. Service Connection `sc-seed-intune` und Federated Credential
5. Checks „Branch control“ und „Required template“ (vorhandene mit anderen Werten werden aktualisiert)
6. Environment `seed-intune` mit Freigabe durch `-Approvers` (Pflicht)
7. Repo `seed-tenant`; ohne `main` mit dem Startinhalt aus [`tenant/`](tenant/) (leere Registerdatei, `azure-pipelines.yml`, README). Ein vorhandenes Repo bleibt unverändert; geprüft wird nur, dass `azure-pipelines.yml` das Template auf `-SeedPipelinesVersion` einbindet.
8. Pipeline `seed-intune`, berechtigt für Service Connection, Environment und GitHub-Service-Connection
9. Branch-Policies auf `main`: Build-Validierung und `-MinimumReviewers` (Standard 1)

Das Konto braucht Privileged Role Administrator (oder Global Administrator) für die Zustimmung zur Graph-Berechtigung, in der Subscription Owner (oder Contributor und User Access Administrator) und Projektadministrator in Azure DevOps. Für `-DryRun` reicht Lesezugriff.

Das Skript startet die Pipeline nicht; mit leerer Registerdatei hätte sie nichts zu tun. Der erste Lauf folgt auf den Merge des ersten Pull Requests mit einem Eintrag.

**Neue Version von seed-pipelines:** Erst das Onboarding mit der neuen `-SeedPipelinesVersion` ausführen (aktualisiert „Required template“), dann `ref` in `seed-tenant/azure-pipelines.yml` per Pull Request anheben. Dazwischen lehnt der Check Läufe mit dem alten Tag ab; PR-Läufe sind nicht betroffen, weil sie die Service Connection nicht nutzen.

### Grenzen

- Eine Richtlinie mit einer Zuweisung (alle Benutzer) für alle Erweiterungen; einzelne Erweiterungen nur für bestimmte Gruppen gibt es nicht.
- Die Einstellungen der Erweiterungen selbst (`managed_schema.json`, unter Windows `HKLM\Software\Policies\Microsoft\Edge\3rdparty\extensions\<id>\policy`) sind nicht Teil davon; sie bleiben bei den Projekten.
- Nur Windows (Einstellungskatalog, MDM). Die Konfliktprüfung sieht nur Richtlinien des Einstellungskatalogs, keine Administrative Templates und keine Custom-Profile (OMA-URI).
- Änderungen von Hand in Intune setzt erst der nächste Lauf zurück; die Pipeline läuft nur bei Änderungen am Repo und von Hand, nicht nach Zeitplan.

## Release

Neue Projekte bekommen die Versionen, die hier als Standard stehen. Vor jedem Tag deshalb anheben:

- `onboarding/Initialize-SeedTenant.ps1`: `$SeedPipelinesVersion`, landet im Repo `seed-scaffold` und damit in jedem neuen Projekt;
- `templates/scaffold.yml`: `pipelinesVersion` und `terraformModuleVersion` (Modul `ado-project`);
- `onboarding/Initialize-SeedIntune.ps1`: `$SeedPipelinesVersion`, bestimmt die Prüfung „Required template“ und den Startinhalt von `seed-tenant`;
- die Beispiele in dieser README.

Danach in jedem bestehenden `seed-scaffold`-Repo `ref` und `pipelinesVersion` in `azure-pipelines.yml` nachziehen und in seed-template den `ref` in `azure-pipelines.yml`. Für `seed-tenant` erst `Initialize-SeedIntune.ps1` mit dem neuen Tag ausführen, dann `ref` per Pull Request anheben (siehe Intune).

## Lizenz

MIT, siehe [LICENSE](LICENSE).
