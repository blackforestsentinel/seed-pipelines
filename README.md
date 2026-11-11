# Sentinel Seed – Pipeline-Templates

Azure-DevOps-Pipeline-Templates für Projekte aus dem [Sentinel-Seed-Template](https://github.com/blackforestsentinel/seed-template). Projekte binden sie per `extends` ein und pinnen einen Tag:

```yaml
# Nur von Hand ankreuzen: erlaubt einem Lauf, Daten zu löschen (siehe Schutz vor Datenverlust)
parameters:
  - name: confirmDataDeletion
    displayName: Datenlöschung bestätigen (Storage, Key Vault, Logs)
    type: boolean
    default: false
  # Nur von Hand ankreuzen: Breaking Changes am Custom Connector zulassen (siehe Prüfung des
  # Custom Connectors); nur mit features.customConnector
  - name: allowBreakingConnectorChanges
    displayName: Breaking Changes am Custom Connector zulassen
    type: boolean
    default: false

resources:
  repositories:
    - repository: seed
      type: github
      name: blackforestsentinel/seed-pipelines
      ref: refs/tags/v0.19.0
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
    allowBreakingConnectorChanges: ${{ parameters.allowBreakingConnectorChanges }}
    # checkConnectorConnections: true             # Verbindungen des Connectors prüfen, braucht Rechte
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
    # treatWarningsAsErrors: false                # Warnungen nicht als Fehler, siehe Prüfungen im Build
```

## templates/web-app.yml

| Stage | Inhalt |
| --- | --- |
| `build` | Ein Job: Abgleich mit `project.yaml` (Projektname, `hosting.staticWebApp: none` passend zu `frontend: false`), der Hinweis auf veraltetes Seed, `terraform fmt` und `validate`, der Terraform-Sicherheitsscan, die eigenen Schritte aus `preBuildSteps`, die Prüfungen im Build (offene `TODO(onboarding)`, Formatierung, NuGet- und npm-Pakete; siehe Prüfungen im Build), `dotnet build` mit Warnungen als Fehler, `dotnet test` und `dotnet publish` der API samt OpenAPI-Dokument als Artefakt `openapi` (mit `Bfs.Seed.OpenApi`), `npm test` und `npm run build` des Frontends (nur mit Frontend), mit `extension` Build, Tests und Manifest-Prüfung der Browser-Erweiterung, danach die eigenen Schritte aus `extraBuildSteps`; im PR-Lauf zusätzlich der Secret-Scan |
| `plan_<env>` | `terraform plan` gegen den Remote-State `<project>/<env>.tfstate`; merkt sich, ob sich die Infrastruktur ändert. Bei Änderungen liegt der Plan neben dem State (siehe Plan neben dem State), ohne Änderungen kommen die Outputs als Artefakt `tfoutputs_<env>`. Mit `features.customConnector` prüft danach der Job `connector_check`, ob die neue OpenAPI-Definition Flows bricht (siehe Prüfung des Custom Connectors) |
| `apply_<env>` | Nur bei Änderungen: Deployment-Job auf das Environment `<project>-<env>` (dort hängt die Freigabe), `terraform apply` des geprüften Plans, danach die Outputs als Artefakt `tfoutputs_<env>` |
| `deploy_<env>` | Deployment-Job auf das Environment `<project>-<env>-app`, ohne Checkout (außer mit `extension.signing`) und ohne `terraform init`: Outputs aus `tfoutputs_<env>`, `config.json` aus dem Output `frontend_config`, `__API_ORIGIN__` in der CSP ersetzen, Function und Static Web App deployen, Status eigener Domains melden, Smoke-Test, mit `features.customConnector` der Custom Connector, mit `extension` das Paket der Browser-Erweiterung als Artefakt `extension_<env>`, mit `extension.signing` zusätzlich signiert und im Projekt-Storage veröffentlicht (Artefakt `extension_release_<env>`) |

Ohne Infrastruktur-Änderungen entfällt `apply_<env>` samt Freigabe. Ein Plan, der nur Ressourcen im State verschiebt (`moved`-Blöcke nach einem Update von seed-terraform), zählt nicht als Änderung: Terraform wiederholt die Verschiebung bei jedem Plan und speichert sie mit dem nächsten echten Apply. `deploy_<env>` lässt sich nach einem Fehler einzeln neu starten, weil sie keinen Plan anwendet. Scheitert `apply_<env>` an einem veralteten Plan, braucht es einen neuen Lauf.

Umgebungen laufen in der Reihenfolge der Liste nacheinander.

### Plan neben dem State

Eine Plan-Datei enthält den State samt aller sensiblen Werte im Klartext, etwa den Deployment-Token der Static Web App. Als Artefakt am Lauf könnte sie jeder laden, der die Pipeline lesen darf, und mit dem Token eigene Inhalte auf die Web-App deployen. Der Plan liegt deshalb im Container des Terraform-States unter `plans/<project>/<env>/<Build.BuildId>.tfplan`; dort kommt nur hin, wer auch den State lesen darf. `apply_<env>` lädt ihn von dort und löscht ihn nach dem Apply. Pläne, die nie angewendet werden (Freigabe abgelehnt oder abgelaufen), löscht der nächste Plan derselben Umgebung nach 31 Tagen. Die Deployment-Identität braucht dafür keine weiteren Rechte: `Storage Blob Data Contributor` auf dem Container hat sie aus dem Onboarding.

Den Deployment-Token selbst setzt `deploy_<env>` bei jedem Deploy zurück (`az staticwebapp secrets reset-api-key`), unmittelbar bevor er ihn für den Deploy der Static Web App liest. `azurerm_static_web_app` liest den Token beim Plan mit; seine Kopien im State und in Plan-Dateien gelten deshalb nach dem nächsten Deploy der Umgebung nicht mehr. Der neue Token steht nur in einer geheimen Variable des Jobs. Ein anderer Weg, der mit dem Token deployt (etwa eine zweite Pipeline), funktioniert damit nicht mehr.

Bis v0.10.0 lag der Plan als Artefakt `tfplan_<env>` am Lauf. Die Tokens in diesen alten Artefakten werden mit dem ersten Deploy ab v0.15.0 ungültig; wer eine ältere Version nutzt, setzt sie nach dem Umstieg von Hand zurück (`az staticwebapp secrets reset-api-key --name <swa> --resource-group <rg>`). Die übrigen Schlüssel im State (Storage, Application Insights) sind ohne Wirkung, weil die Seed-Module Shared Key und lokale Anmeldung abschalten.

### Laufzeit

Ein Lauf ohne Infrastruktur-Änderung dauert rund 4 Minuten (vorher 7). Die Organisation hat einen parallelen Job; deshalb gibt es im Build nur einen Job statt dreier, die ohnehin nacheinander liefen. Das .NET SDK des Images genügt, wenn es zu `global.json` passt; sonst installiert `UseDotNet` es. `tfoutputs_<env>` (Outputs ohne sensible Werte, dazu `project.yaml`) erspart dem Deploy das `terraform init`.

Die Function geht über Kudu (`/api/publish`) mit einem Entra-Token der Service Connection raus, nicht über `az functionapp deployment source config-zip`: Das wartet nach dem Deploy fest 60 Sekunden und prüft dann den Host. Kudu verarbeitet das Paket rund eine Minute lang; währenddessen läuft der Deploy der Static Web App, danach wartet die Pipeline auf den Status des Deployments. Ob die neue Fassung läuft, prüft der Smoke-Test über die Build-Nummer. Eine frisch angelegte Function antwortet einige Minuten mit 503, bis die Storage-Rollen ihrer Identität wirken; der Deploy versucht es dann bis zu 10 Minuten lang erneut. Scheitert der Function-Deploy endgültig, ist die Static Web App schon neu; ein erneuter Lauf von `deploy_<env>` gleicht das aus.

Caching (NuGet, npm, Terraform-Provider) fehlt bewusst: Auf den gehosteten Agents ist der NuGet-Cache rund 1 GB groß und der Provider-Cache 670 MB, beides wiederherzustellen dauert länger als `dotnet restore` (7 bis 15 s) und `terraform init` (7 s) ohne Cache. `npm ci` war auch mit Cache-Treffer nicht schneller, und jeder neue Cache-Schlüssel kostet beim Sichern rund eine Minute. Für den Cache brauchen Projekte deshalb keine NuGet-Lockfiles; mit privatem Feed empfehlen sie sich trotzdem (siehe Privater Paket-Feed).

### Terraform und Provider

`templates/steps/terraform-install.yml` lädt Terraform in der Version `terraformVersion` in jedem Job von releases.hashicorp.com und prüft das Zip per SHA-256, wie gitleaks im Secret-Scan: die Standardversion gegen die Prüfsumme im Template (`pinnedVersion`, `pinnedSha256`), jede andere Version gegen `terraform_<version>_SHA256SUMS` von derselben Seite. Ein `terraform`, das schon im `PATH` oder im Tool-Verzeichnis liegt, nutzt die Pipeline nicht, und das Verzeichnis, das sie in den `PATH` stellt, legt sie jedes Mal neu an: Auf eigenen Agents könnte ein früherer Lauf dort ein anderes Programm abgelegt haben. Wer die Standardversion in `web-app.yml` und `scaffold.yml` anhebt, ändert `pinnedVersion` und `pinnedSha256` mit (Prüfsumme für `linux_amd64` aus `SHA256SUMS`, dessen Signatur HashiCorp mit `SHA256SUMS.sig` veröffentlicht).

Liegt `.terraform.lock.hcl` im Ordner `infra` (seed-template legt sie an), läuft `terraform init` mit `-lockfile=readonly`, im Build wie in Plan und Apply. Provider kommen dann nur in den Versionen und mit den Prüfsummen aus der Datei; fehlt ein Provider darin oder passt die Version nicht mehr zu den Anforderungen, bricht init ab, schon im PR-Lauf, statt die Datei still zu ergänzen. Die Datei ändert das Projekt per Pull Request, etwa nach einem Update von seed-terraform mit `terraform init -upgrade` und `terraform providers lock -platform=linux_amd64 -platform=windows_amd64` (Prüfsummen für die Agents und für Windows-Rechner im Team). Ohne die Datei läuft init wie bisher.

### PR-Validierung

Läufe mit `Build.Reason` `PullRequest` (ausgelöst von der Branch-Policy auf `main`, die `seed-scaffold` anlegt und `Initialize-SeedProject.ps1` nach dem Umschalten setzt) bestehen nur aus `build`: Build, Tests, die Prüfungen im Build (unten), `terraform fmt` und `validate` ohne Backend, der Terraform-Sicherheitsscan, der Hinweis auf veraltetes Seed und ein Secret-Scan. Plan, Apply und Deploy entstehen gar nicht erst, der Lauf braucht also weder die Azure-Service-Connection noch Environments. Es bleibt bei einer Pipeline-Definition je Projekt; eine zweite bräuchte eigene Freigaben für Environments und Service Connection. `pr: none` im Projekt bleibt stehen: In Azure Repos startet die Branch-Policy PR-Läufe, nicht der Trigger.

Mit `validationOnly: true` läuft dieselbe Prüfung ohne PR, etwa um sie von Hand nachzustellen.

Der Secret-Scan nutzt [gitleaks](https://github.com/gitleaks/gitleaks) in fester Version, der Download wird per SHA-256 geprüft. Er prüft alle Commits des PRs (`HEAD^1..HEAD^2` des Merge-Commits), nicht nur den Endstand: Ein Secret, das ein späterer Commit entfernt, bliebe sonst in der Historie. Gefundene Werte erscheinen geschwärzt im Log. In Läufen auf `main` scannt die Pipeline nicht: Mit der Pflicht-Policy kommt jede Änderung über einen PR.

Was bei einem Fund zu tun ist, steht auch in der Fehlermeldung:

- **Ein weiterer Commit, der das Secret löscht, reicht nicht.** Der Scan bleibt rot, weil das Secret in einem Commit des PRs steht und mit dem Merge in die Historie von `main` käme.
- **Branch neu schreiben**, ohne den betroffenen Commit: etwa `git rebase -i <basis>`, den Commit bearbeiten (`edit`) oder verwerfen (`drop`), dann `git push --force-with-lease`. Das braucht in Azure Repos „Force push“ am Branch; wer ihn angelegt hat, hat es meist. Alternativ einen neuen Branch ohne den Commit anlegen und den PR neu stellen.
- **Secret widerrufen** und beim Anbieter ersetzen, auch nach dem Umschreiben: Es lag schon im Repo und gilt damit als bekannt.
- **Falscher Alarm:** den Fingerprint aus dem Log in `.gitleaksignore` im Projekt eintragen; eigene Regeln gehören in `.gitleaks.toml`.

### Terraform-Sicherheitsscan

Im Build-Job, auch im PR-Lauf, prüft [Trivy](https://github.com/aquasecurity/trivy) (`trivy config`, [`templates/steps/terraform-scan.yml`](templates/steps/terraform-scan.yml)) die `.tf`-Dateien unter `infraPath` statisch auf Fehlkonfigurationen, etwa öffentlichen Blob-Zugriff, TLS unter 1.2, nicht erzwungenes HTTPS, FTP an App Services oder einen Key Vault ohne Purge-Schutz. Der Scan läuft nach `terraform init -backend=false` und dauert mit Download rund 3 bis 7 Sekunden.

- **Was gemeldet wird:** Befunde in den Dateien des Projekts, also im Wurzelmodul und in lokalen Modulen (`source = "./modules/…"`). Trivy wertet die heruntergeladenen Module aus `.terraform/modules` mit aus, damit Werte aus Modul-Outputs bekannt sind, meldet deren Befunde aber nicht (`--tf-exclude-downloaded-modules`): Ein Projekt könnte sie weder beheben noch begründet ausnehmen. Die Module aus seed-terraform prüft deren Repo; Module anderer Herkunft verantwortet das Projekt, das sie einbindet.
- **Schwere:** `CRITICAL` und `HIGH` sind Fehler und brechen den Build, `MEDIUM`, `LOW` und `UNKNOWN` sind Warnungen. Einige `MEDIUM`-Regeln sind trotzdem Fehler, weil sie Klartext-Verbindungen oder endgültigen Datenverlust bedeuten (`errorChecks`): AZU-0016 (Key Vault ohne Purge-Schutz), AZU-0020 und AZU-0026 (Datenbank ohne TLS bzw. unter 1.2), AZU-0071 (App Service mit FTP), AZU-0072 (Web App mit HTTP).
- **Seed-Regelsatz** (`skipChecks`, aus für alle Projekte, Gründe im Template und im Log):

  | Regel | Schwere | Warum aus |
  | --- | --- | --- |
  | AZU-0012 | CRITICAL | Storage-Netzwerkregeln mit Deny: Seed betreibt keine VNets, die Function erreicht Storage über den öffentlichen Endpunkt, geschützt durch Entra ID ohne Shared Key |
  | AZU-0013 | CRITICAL | `network_acls` am Key Vault: wie AZU-0012, Zugriff per RBAC |
  | AZU-0009, AZU-0057 | MEDIUM | klassische Storage-Analytics-Protokolle, veraltet; Protokolle laufen über Diagnoseeinstellungen |
  | AZU-0058 | LOW | Georedundanz: Kostenentscheidung; Seed schützt Daten mit Soft Delete, Versionierung und Löschsperre |
  | AZU-0060 | MEDIUM | kundenseitig verwaltete Schlüssel: Compliance-Vorgabe, keine Grundsicherung |
  | AZU-0061 | MEDIUM | doppelte Verschlüsselung: Compliance-Option |
  | AZU-0003 | MEDIUM | App Service Authentication: Seed prüft Tokens in der API (`Bfs.Seed.Auth`) |
  | AZU-0001 | LOW | Client-Zertifikate: Seed authentifiziert mit Entra-Tokens |
  | AZU-0015 | LOW | `content_type` an Secrets: Beschreibung ohne Schutzwirkung |
  | AZU-0017 | LOW | Ablaufdatum an Secrets: Seed erzwingt keines, Werte setzt ein Mensch außerhalb von Terraform |

- **Ausnahmen** stehen inline im `.tf`, als Kommentar direkt über dem Block oder der Zeile mit dem Befund (oder am Ende dieser Zeile), mit Regel-ID und Begründung in derselben Zeile. So erscheinen sie im Diff des PRs, und der Scan listet sie im Log:

  ```hcl
  # trivy:ignore:AZU-0011 Lesegerät im Lager kann nur TLS 1.0, Austausch bis Ende 2027
  resource "azurerm_storage_account" "scanner" {
    min_tls_version = "TLS1_0"
    # ...
  }

  # Mit Ablaufdatum: ab 2028 wieder ein Befund
  # trivy:ignore:AZU-0011:exp:2027-12-31 Lesegerät im Lager kann nur TLS 1.0
  ```

  Die Regel-ID steht im Befund (`AZU-0011`); die lange Form (`azure-storage-use-secure-tls-policy`) geht auch, ebenso mehrere Regeln in einer Zeile; die Begründung steht hinter der letzten. Ein Befund kann mehrere Regeln auslösen, etwa TLS 1.0 an Storage AZU-0011 und AZU-0059; jede braucht ihre Ausnahme.

  Trivy liest Ausnahmen freier, als das Beispiel zeigt: in jeder Zeile einer `.tf`-, `.tofu`-, `.tf.json`- oder `.tofu.json`-Datei, auch ohne Kommentarzeichen, in Blockkommentaren und Strings, als `trivy:` oder `tfsec:`, mit den Abschnitten `ignore`, `exp` und `ws` in beliebiger Reihenfolge. Die Prüfung liest sie nach dem Parser von Trivy genauso und verlangt von jedem Wort, das mit `trivy:` oder `tfsec:` beginnt:

  - eine Ausnahme mit Regel (`ignore:<Regel>`); ein Wort wie `trivy:exp:…` ohne Regel oder ein `trivy:` im Text ist ein Fehler;
  - keinen Platzhalter: Trivy liest `*` in einer Regel oder einem Workspace (`ws`) als beliebiges Stück, `trivy:ignore:AZU-*` schaltete eine ganze Familie ab und `trivy:ignore:*` alles. Jedes `*` in `ignore` oder `ws` ist deshalb ein Fehler;
  - eine Begründung mit mindestens zehn Zeichen dahinter, in derselben Zeile.
- **Nur Einstellungen des Templates:** `.trivyignore`, `.trivyignore.yaml` und `trivy.yaml` im Projekt wirken nicht (der Scan warnt, wenn es sie gibt), Umgebungsvariablen `TRIVY_*` ebenso wenig. Die Regeln stecken im Programm (`--skip-check-update`) und ändern sich nur mit dessen Version; der Scan braucht außer dem Download kein Netz und sendet keine Telemetrie.
- **Version:** Trivy kommt wie gitleaks in fester Version mit SHA-256 aus `trivy_<version>_checksums.txt` (Parameter `version` und `sha256` des Schritt-Templates). Beim Anheben eine Version nehmen, die mindestens zwei Wochen alt ist, die Prüfsumme mit dem Digest des Release-Assets auf GitHub abgleichen und den Scan gegen seed-template und die Module von seed-terraform laufen lassen: Neue Regeln können neue Fehler bringen. Dazu den Parser der Ausnahmen (`pkg/iac/ignore/parse.go`) mit dem Nachbau in `terraform-scan.yml` und der Gegenprobe in `tests/steps/test_terraform_scan.py` abgleichen.
- **Abdeckung:** Trivy kennt die `azurerm`-Ressourcen. Ressourcen über `azapi` (etwa die Function mit Flex Consumption im Modul `core`) prüft es nicht.

### Hinweis auf veraltetes Seed

Ebenfalls im Build-Job, auch im PR-Lauf, vergleicht [`templates/steps/seed-versions.yml`](templates/steps/seed-versions.yml), was das Projekt nutzt, mit dem neuesten Release, und warnt je Quelle, wenn etwas zurückliegt:

| Was | Woher im Projekt | Neueste Version |
| --- | --- | --- |
| seed-pipelines | `ref` der Repository-Ressource `seed` in `azure-pipelines.yml`, zur Laufzeit (`resources.repositories.seed.ref`) | Tags per `git ls-remote` |
| Module aus seed-terraform | `?ref=vX.Y.Z` in den `.tf`-Dateien unter `infraPath` | Tags per `git ls-remote` |
| `Bfs.Seed.*` | `PackageReference` und `PackageVersion` in `.csproj`, `.props`, `.targets` | `https://api.nuget.org/v3-flatcontainer/<id>/index.json` |
| `@blackforestsentinel/seed-web-*` | `package.json`, aufgelöst aus `package-lock.json` | npm-Registry |

Neueste Version ist die höchste ohne Vorabkennung (`-preview`, `-rc` …). Branches statt Tags (`refs/heads/…`, `?ref=main`) vergleicht der Schritt nicht, er nennt sie nur im Log. Eine Paketversion aus einer MSBuild-Property (`Version="$(SeedVersion)"`) löst er über die PropertyGroups der `.csproj`-, `.props`- und `.targets`-Dateien auf, etwa aus `Directory.Build.props`, wenn die Property genau einen festen Wert hat. Bleibt eine Paketversion offen (Property ohne eindeutigen Wert, Bereich wie `0.9.*`, `file:` bei npm), warnt er „Version nicht prüfbar“. Er bricht nie den Build: Ist GitHub, NuGet oder npm nicht erreichbar, steht das als Zeile im Log, ohne Warnung. Er dauert ein bis drei Sekunden. Die Repository-Ressource muss `seed` heißen, wie in den Beispielen; sonst entfällt der Vergleich für seed-pipelines.

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

- **Reihenfolge:** nach Checkout, Abgleich mit `project.yaml`, Hinweis auf veraltetes Seed, `terraform validate`, Terraform-Sicherheitsscan und dem .NET SDK laut `global.json`, vor `dotnet restore`. Node in der Version `nodeVersion` richtet das Template erst danach ein; bis dahin gilt die des Images.
- **PR-Lauf:** Die Schritte laufen auch im PR-Lauf und mit `validationOnly`, denn ohne Restore gibt es keinen Build.
- **Rechte:** `NuGetAuthenticate@1` und `npmAuthenticate@0` melden sich ohne Secret mit dem Job-Token an, also als `<Projekt> Build Service (<org>)` oder, wenn das Projekt den Job-Scope nicht auf sich beschränkt, als `Project Collection Build Service (<org>)`. Diese Identität braucht am Feed mindestens die Rolle Feed Reader (Feed settings, Permissions). Liegt der Feed in einem anderen Projekt, braucht sie zusätzlich Lesezugriff auf dieses Projekt (Gruppe Readers). Für Feeds einer anderen Organisation oder außerhalb von Azure DevOps nennt der Schritt eine NuGet-Service-Connection des Projekts (`nuGetServiceConnections`). Eine Azure-Service-Connection bekommen die Schritte nicht.
- Für die Anmeldung taugt `extraBuildSteps` nicht: Diese Schritte laufen erst nach Build und Tests.
- **Lücken:** Die NuGet-Prüfung fragt immer nuget.org, die npm-Prüfung die Registry aus `.npmrc`; Azure Artifacts leitet `npm audit` an npmjs weiter (siehe Prüfungen im Build).

**Dependency Confusion:** Kennt der Restore neben dem privaten Feed auch nuget.org oder npmjs.com, kann ein öffentliches Paket mit dem Namen eines internen (und höherer Version) das interne ersetzen; sein Code liefe dann im Build und in der App. Deshalb legt jedes Paket fest, woher es kommt:

- **NuGet, eine Quelle:** In `nuget.config` mit `<clear />` nur den privaten Feed nennen und dort nuget.org als Upstream-Quelle eintragen (Azure Artifacts, Feed settings, Upstream sources). Der Feed löst dann jeden Namen selbst auf, und ein intern veröffentlichtes Paket verdeckt ein öffentliches gleichen Namens.

  ```xml
  <configuration>
    <packageSources>
      <clear />
      <add key="intern" value="https://pkgs.dev.azure.com/<org>/_packaging/<feed>/nuget/v3/index.json" />
    </packageSources>
  </configuration>
  ```

- **NuGet, mehrere Quellen:** `packageSourceMapping` ordnet Namensmuster genau einer Quelle zu; das spezifischere Muster gewinnt, interne Präfixe kommen also nie von nuget.org.

  ```xml
  <packageSourceMapping>
    <packageSource key="nuget.org"><package pattern="*" /></packageSource>
    <packageSource key="intern"><package pattern="Example.*" /></packageSource>
  </packageSourceMapping>
  ```

- **npm:** Interne Pakete haben einen eigenen Scope, und `.npmrc` leitet nur diesen Scope an den privaten Feed (`@example:registry=https://pkgs.dev.azure.com/<org>/_packaging/<feed>/npm/registry/`), alles andere an `registry=https://registry.npmjs.org/`. Pakete ohne Scope gehören nicht in den privaten Feed. `package-lock.json` mit `integrity` hat jedes Projekt ohnehin (`npm ci`).
- **Lockfiles für NuGet:** Projekte mit privatem Feed setzen `RestorePackagesWithLockFile` (etwa in `Directory.Build.props`) und checken `packages.lock.json` ein; mit `<RestoreLockedMode Condition="'$(TF_BUILD)' == 'true'">true</RestoreLockedMode>` restauriert die Pipeline nur, was darin steht (wie `dotnet restore --locked-mode`), samt Prüfsummen, und bricht bei Abweichungen ab.

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

### Prüfungen im Build

Viele Projekte entstehen mit Coding-Agents. Dann wirkt, was den Build bricht; ein Hinweis in der README wirkt kaum. Der Build-Job prüft deshalb die API und die npm-Pakete, auch im PR-Lauf und mit `validationOnly`. Ausnahmen gibt es, aber nur im Projekt und mit Begründung, damit sie im PR zu sehen sind.

| Prüfung | Ergebnis | Ausnahme |
| --- | --- | --- |
| Warnungen des Compilers, zu Nullable, der Analyzer und von MSBuild-Tasks in `dotnet build` und `dotnet publish` | Fehler | je Warnung im Projekt (unten); alle mit `treatWarningsAsErrors: false` |
| Direkte NuGet-Referenz oder direkte npm-Laufzeit-Abhängigkeit in einer Vorabversion (`-preview`, `-rc`, `-beta` …) | Fehler | `build.prereleasePackages` in `project.yaml` |
| Bekannte Lücke High oder Critical, auch in transitiven Paketen (bei npm nur in Laufzeit-Abhängigkeiten) | Fehler | `build.ignoreAdvisories` in `project.yaml` |
| Bekannte Lücke Low oder Moderate | Warnung | – |
| `package.json` mit Laufzeit-Abhängigkeiten, aber ohne `package-lock.json` | Warnung | – |
| Veraltete direkte NuGet-Referenz, neuere Version derselben Hauptversion (außer `Bfs.Seed.*`, die meldet „Seed-Versionen prüfen“) | Warnung | – |
| Neue Hauptversion einer direkten NuGet-Referenz | Zeile im Log | – |
| `RS0030` (verbotene APIs) oder eine Regel aus Bfs.Seed (`SEEDAUTH…`, `SEEDSUPPRESS…`, `SEEDOPENAPI…`) abgeschaltet | Fehler | Kommentar mit dem Grund in der Zeile davor |
| Analyzer pauschal abgeschaltet (`RunAnalyzers`, `dotnet_analyzer_diagnostic.severity` …) | Warnung | – |
| Formatierung laut `.editorconfig` (`dotnet format whitespace`) | Fehler | `.editorconfig` |
| `TODO(onboarding)` in `project.yaml` | Warnung je Zeile | – |

Die Schritte „Offene TODOs in project.yaml“, „Abgeschaltete Regeln prüfen“, „Formatierung prüfen“ und „NuGet-Pakete prüfen“ ([`templates/steps/dotnet-checks.yml`](templates/steps/dotnet-checks.yml)) laufen nach `dotnet restore` und vor `dotnet build`: Sie sind schnell und brauchen keinen Build. Danach richtet das Template Node in `nodeVersion` ein (mit Frontend oder `extension`), und „npm-Pakete prüfen“ ([`templates/steps/npm-checks.yml`](templates/steps/npm-checks.yml)) folgt, ebenfalls vor `dotnet build` und vor jedem `npm ci`. Die Logik steht mit Tests in [`scripts/dotnet/checks.py`](scripts/dotnet/checks.py). Dafür checkt der Build-Job seed-pipelines aus (`checkout: seed` nach `$(Pipeline.Workspace)/seed`, dieselbe `ref` wie die Templates); das Projekt bleibt mit `path: s` in `$(Build.SourcesDirectory)`, dem Arbeitsverzeichnis aller Schritte. seed-pipelines kommt zuerst („seed-pipelines auschecken (Skripte der Prüfungen)“, dann „Projekt auschecken“): Der Agent legt beim Jobstart `s/<repo>` je Repo an, und der Checkout nach `s` löscht `s` vorher ganz. In der umgekehrten Reihenfolge fand der zweite Checkout sein Verzeichnis nicht mehr und warnte „Unable move and reuse existing repository to required location“. Werte aus dem Repo und die Ausgaben von `dotnet` und `npm` kommen nur bereinigt ins Log. Lokal gemessen dauern die vier Schritte der API zusammen rund 15 Sekunden (Pakete 10, Formatierung 5), die npm-Prüfung 2 bis 4 Sekunden je npm-Projekt; auf gehosteten Agents kommen mit dem Checkout etwa 20 bis 40 Sekunden zum Lauf dazu.

**Warnungen als Fehler** (`treatWarningsAsErrors`, Standard `true`): Der Schritt „dotnet build“ baut die Solution mit `-warnaserror`, ebenso `dotnet publish`; `dotnet test` nutzt danach den fertigen Build (`--no-build`). Einen eigenen Build braucht es, weil `dotnet test` mit Microsoft.Testing.Platform Schalter wie `-warnaserror` an die Tests weitergibt statt an MSBuild. Der Schalter gilt für den ganzen Build: Warnungen des Compilers, zu Nullable und der Analyzer, Warnungen von MSBuild-Tasks (etwa `SEEDOPENAPI…` aus `Bfs.Seed.OpenApi`) und die Warnungen des Restores, die der Build aus `project.assets.json` wiederholt. `<TreatWarningsAsErrors>false</TreatWarningsAsErrors>` im Projekt hebt ihn nicht auf. Ausnahmen gelten je Warnung und stehen im Projekt, also im PR:

- `[SuppressMessage("Usage", "CA2200", Justification = "…")]` an der Stelle im Code. Eine Unterdrückung ohne `Justification` meldet ein Analyzer aus den `Bfs.Seed`-Paketen als Fehler.
- `.editorconfig`, mit Kommentar zum Grund und möglichst auf Pfade beschränkt, etwa Unterstriche in Testnamen:

  ```ini
  # Testnamen wie Methode_Fall_Ergebnis
  [**/*.Tests/**.cs]
  dotnet_diagnostic.CA1707.severity = none
  ```

- `NoWarn` (Warnung fällt weg) oder `WarningsNotAsErrors` (bleibt als Warnung sichtbar) in der Projektdatei oder in `Directory.Build.props`, mit Kommentar zum Grund; gilt für das ganze Projekt. Für eine einzelne Paketreferenz geht auch `<PackageReference … NoWarn="NU1701" />`.

Für `RS0030` und die Regeln aus Bfs.Seed ist der Kommentar Pflicht, siehe Abgeschaltete Regeln.

Diese Warnungen bleiben auch mit `-warnaserror` Warnungen (`-warnNotAsError`):

| Code | Warum keine Fehler |
| --- | --- |
| `NU1901` bis `NU1904` | Lücken; die prüft „NuGet-Pakete prüfen“ nach Schwere und mit den Ausnahmen aus `project.yaml`. Der Schritt `dotnet restore` läuft deshalb mit `-p:NuGetAudit=false`: Setzt ein Projekt selbst `TreatWarningsAsErrors`, brächen die Lücken sonst schon den Restore, ohne Rücksicht auf die Ausnahmen |
| `NU1603`, `NU1801`, `NU1900`, `NU1905` | hängen vom Zustand der Feeds und des Netzes ab (Version fehlt im Feed, Quelle oder Daten zu Lücken nicht erreichbar) und würden Builds zufällig brechen |
| `NU1608` | Paket außerhalb der Versionsgrenze einer Abhängigkeit; entsteht oft gerade dann, wenn ein Projekt ein transitives Paket wegen einer Lücke selbst anhebt |
| `MSB3026` | Kopieren wird wiederholt, eine vorübergehende Sperre |

Die übrigen Warnungen von NuGet sind Fehler: Sie hängen an der Konfiguration des Projekts und lassen sich dort beheben oder begründet abschalten, etwa `NU1507` (mehrere Paketquellen ohne `packageSourceMapping`, siehe Privater Paket-Feed), `NU1510` (überflüssige Paketreferenz) oder `NU1701` (Paket nur für .NET Framework). Das gilt auch für den inneren Build der Function (`WorkerExtensions`), den das Functions-SDK während des Builds wiederherstellt.

**NuGet-Pakete** („NuGet-Pakete prüfen“), drei Abfragen mit `--format json`:

- `dotnet list <solution> package`: direkte Referenzen in einer Vorabversion. Maßgeblich ist die Version, die der Restore aufgelöst hat, nicht der Text in der Projektdatei; das gilt also auch für Central Package Management (`Directory.Packages.props`, `VersionOverride`) und Versionsbereiche. Transitive Pakete und die impliziten Pakete des SDK zählen nicht.
- `dotnet list <solution> package --vulnerable --include-transitive --source https://api.nuget.org/v3/index.json`: bekannte Lücken aus der GitHub Advisory Database. Immer gegen nuget.org: Ein privater Feed liefert keine Daten zu Lücken, und `dotnet` meldet dann still keine. Geladen wird nur die Liste der Lücken, kein Paket.
- `dotnet list <solution> package --outdated`: neuere Versionen direkter Referenzen in den Quellen aus `nuget.config`, also mit der Anmeldung aus `preBuildSteps`. Eine Warnung gibt es nur, wenn es in der genutzten Hauptversion etwas Neueres gibt („Veraltet: … neueste Version derselben Hauptversion …“). Eine neue Hauptversion steht nur als Zeile im Log („Neue Hauptversion verfügbar: X 9.0.0 → 10.0.14“), ohne Annotation: Oft lässt sie sich nicht nehmen, etwa weil ein anderes Paket nur die alte Hauptversion unterstützt, und eine Warnung in jedem Lauf, die niemand beheben kann, lehrt nur, Warnungen zu übersehen. Gibt es beides, kommen Warnung und Logzeile. Hat ein Paket eine neue Hauptversion, fragt eine zweite Abfrage mit `--outdated --highest-minor` die neueste Version derselben Hauptversion ab; sonst bleibt es bei einer. `Bfs.Seed.*` lässt die Prüfung aus, sobald `dotnet` eine aufgelöste Version liefert: Zurückliegende Seed-Pakete meldet schon der Schritt „Seed-Versionen prüfen“, sonst käme die Warnung doppelt.

Scheitert eine Abfrage am Netz oder an `dotnet` selbst (Feed nicht erreichbar, Anmeldung fehlt, mehr als 5 Minuten), gibt es eine Warnung „… nicht möglich“ statt eines Fehlers: Ein Ausfall von nuget.org soll weder PR noch Deploy aufhalten. Die Einträge in `project.yaml` prüft der Schritt trotzdem.

**npm-Pakete** („npm-Pakete prüfen“): jede `package-lock.json` (oder `npm-shrinkwrap.json`) im Repo, ohne `node_modules`, `bin`, `obj` und `.terraform`. Das sind Frontend und Browser-Erweiterung, aber auch npm-Projekte, die ein Projekt selbst in `extraBuildSteps` baut. Je Ordner mit Lock-Datei:

- `npm audit --json --omit=dev --package-lock-only --no-offline`: bekannte Lücken der Laufzeit-Abhängigkeiten, direkt und transitiv, aus der GitHub Advisory Database. `--package-lock-only` liest nur die Lock-Datei; die Prüfung braucht also kein `npm ci` und läuft davor, bevor Install-Skripte eines Pakets mit bekannter Lücke laufen. `--no-offline`, weil npm mit `offline=true` in einer `.npmrc` gar nicht fragt und still keine Lücken meldet. Maßgeblich ist das JSON, nicht der Exit-Code: Den verschiebt `audit-level` in einer `.npmrc`.
- Schwere und Ausnahmen wie bei NuGet: High und Critical sind Fehler, außer die Advisory steht in `build.ignoreAdvisories`; Low und Moderate sind Warnungen. Gemeldet wird je Paket, nicht je Advisory: Eine alte Version hat bei npm oft ein Dutzend, behoben werden sie mit derselben neuen Version. Die Meldung nennt bis zu acht Kennungen, das Log jede mit Titel und Link. Die Ausnahmen gelten je Advisory; nennt npm dieselbe Advisory für zwei Pakete (etwa eine Bibliothek und ihre eingebaute Abhängigkeit), gilt der Eintrag für beide.
- Vorabversionen: direkte Laufzeit-Abhängigkeiten (`dependencies`, `optionalDependencies`, auch der Workspaces) in einer Vorabversion laut Lock-Datei sind Fehler, außer das Paket steht in `build.prereleasePackages`. Workspaces selbst, Ordner (`file:`) und Git-Repos zählen nicht. Dieser Teil liest nur die Lock-Datei und braucht weder npm noch Netz.
- Workspaces prüft der Aufruf im Ordner der gemeinsamen Lock-Datei mit; Mitglieder ohne eigene Lock-Datei sind damit abgedeckt. Lock-Dateien von npm 6 (`lockfileVersion` 1) gehen auch.
- Ein Ordner mit `package.json` und Laufzeit-Abhängigkeiten, aber ohne Lock-Datei, gibt eine Warnung; `yarn.lock` und `pnpm-lock.yaml` liest `npm audit` nicht. Ordner nur mit `devDependencies` (etwa für Werkzeuge) übergeht die Prüfung.
- **npm** kommt aus `UseNode@1` (Version `nodeVersion`), das das Template mit Frontend oder `extension` vor diesem Schritt einrichtet. Ohne beides nimmt die Prüfung das npm des Agents; gehostete Agents bringen eins mit. Fehlt npm, obwohl es Lock-Dateien gibt, warnt der Schritt „nicht auf Lücken geprüft“; dann Node in `preBuildSteps` einrichten (`- task: UseNode@1` mit `inputs: { version: '24.x' }`). Geprüft mit npm 10, 11 (Node 24) und 12; npm vor Version 7 kennt das Format nicht.
- **Registry:** `npm audit` fragt die Registry aus der npm-Konfiguration des Ordners (`.npmrc`), wie `npm ci`. Ist das nicht registry.npmjs.org, nennt das Log sie: Sie muss Daten zu Lücken liefern. Azure Artifacts leitet die Anfrage an npmjs weiter; dabei gehen, wie bei jedem `npm ci`, die Namen und Versionen aller Pakete an die öffentliche Registry. Ist die Registry nicht erreichbar, verlangt sie eine Anmeldung oder dauert die Antwort länger als 2 Minuten, gibt es eine Warnung „… nicht möglich“ statt eines Fehlers.

```yaml
# project.yaml
build:
  # Vorabversionen direkter Abhängigkeiten (NuGet-Paket-ID oder npm-Paketname, Groß- und
  # Kleinschreibung egal)
  prereleasePackages:
    - name: Some.Package
      reason: Nur die Vorabversion unterstützt X; die stabile Version ist angekündigt.
  # Bekannte Lücken bewusst hinnehmen, NuGet und npm (GHSA-Kennung aus der Meldung oder die URL
  # der Advisory)
  ignoreAdvisories:
    - id: GHSA-xxxx-xxxx-xxxx
      reason: Betrifft nur Y, das die API nicht nutzt.
```

- Ein Eintrag ohne `reason` ist ein Fehler, ebenso einer mit weniger als 10 Zeichen (ohne Leerraum am Rand) und einer, dessen `reason` YAML nicht als Text liest (`no`, `2026-01-01`; dann in Anführungszeichen). Dieselbe Grenze gilt für die Begründungen der Analyzer aus Bfs.Seed und des Terraform-Scans.
- Trifft ein Eintrag weder bei NuGet noch bei npm etwas, warnt „npm-Pakete prüfen“; der Eintrag gehört dann entfernt. So veralten Ausnahmen nicht unbemerkt. Das Ergebnis der NuGet-Prüfung liest der Schritt aus `$(Agent.TempDirectory)/seed-package-checks.json` (Parameter `stateFile` beider Schritt-Templates). Lief eine der Prüfungen nicht vollständig (Netz, kein npm), bleibt es bei einem Hinweis im Log.
- Eine Ausnahme, die greift, steht mit ihrem Grund im Log.
- Eine Lücke in einem transitiven Paket behebt meist eine direkte Referenz auf eine Version ohne die Lücke; bei npm geht auch `overrides` in `package.json`. `npm audit fix` im Ordner des Projekts hebt, was ohne neue Hauptversion geht.

**Grenzen der npm-Prüfung:** Lücken in `devDependencies` (Vite, Vitest, jsdom …) meldet sie nicht; sie laufen nur im Build und auf Entwicklerrechnern, nicht in der ausgelieferten App. Ohne `--omit=dev` wären sie die meisten Meldungen. Sie prüft nur, was in Lock-Dateien steht, und nur Pakete mit Einträgen in der GitHub Advisory Database; ein Paket mit Schadcode, für das es noch keine Advisory gibt, erkennt sie nicht. Veraltete npm-Pakete meldet sie nicht, außer `@blackforestsentinel/seed-web-*` über „Seed-Versionen prüfen“.

**Abgeschaltete Regeln** („Abgeschaltete Regeln prüfen“): `RS0030` (verbotene APIs, `BannedApiAnalyzers`) und die Regeln aus Bfs.Seed (`SEEDAUTH…`, `SEEDSUPPRESS…`, `SEEDOPENAPI…`) darf ein Projekt nur mit Begründung abschalten. Der Schritt durchsucht alle `.editorconfig`, `.globalconfig`, Projektdateien, `.props`, `.targets` und `.rsp` im Repo, ohne `bin`, `obj` und `node_modules`, nach:

- `dotnet_diagnostic.<Regel>.severity` mit `none`, `silent` oder `suggestion`;
- der Regel in `NoWarn`, `WarningsNotAsErrors`, `MSBuildWarningsAsMessages` oder `MSBuildWarningsNotAsErrors`, auch über mehrere Zeilen;
- der Regel in einer Antwortdatei wie `Directory.Build.rsp`.

Die Begründung ist ein Kommentar in der Zeile direkt davor (`#` bzw. `<!-- -->`, auch mehrzeilig) mit mindestens 10 Zeichen. Fehlt er, ist es ein Fehler; sonst steht die Ausnahme mit ihrem Grund im Log. Steht die Regel an anderer Stelle, etwa in einer eigenen Eigenschaft, die später in `NoWarn` landet, gibt es eine Warnung. Pauschal-Abschaltungen gibt es immer als Warnung, auch mit Begründung: `RunAnalyzers` oder `RunAnalyzersDuringBuild` auf `false`, `dotnet_analyzer_diagnostic.severity` (auch je Kategorie) mit schwacher Schwere, `<Analyzer Remove=…>` und `ExcludeAssets` mit `analyzers`. `generated_code = true` warnt nur ohne Begründung: Für diese Dateien laufen keine Analyzer und keine Formatprüfung. `TreatWarningsAsErrors` im Projekt spielt keine Rolle, die Pipeline baut ohnehin mit `-warnaserror`. Andere pauschale Eingriffe, etwa `<WarningLevel>0</WarningLevel>`, prüft der Schritt nicht; sie stehen im PR und fallen nur im Review auf.

```xml
<!-- Hinweise zu Enums prüft ein eigener Test des OpenAPI-Exports -->
<WarningsNotAsErrors>$(WarningsNotAsErrors);SEEDOPENAPI014</WarningsNotAsErrors>
```

**Formatierung** („Formatierung prüfen“): `dotnet format whitespace <solution> --verify-no-changes`, also Einrückung, Leerzeichen, Zeilenumbrüche, Zeilenende und letzte Zeile laut `.editorconfig`. Das prüft sonst niemand; der Teilbefehl braucht keinen Build und dauert wenige Sekunden. `style` und `analyzers` lässt der Schritt bewusst aus: Sie kompilieren die Solution ein zweites Mal und melden dieselben Regeln wie der Build, sobald das Projekt Code-Stil im Build prüft (`EnforceCodeStyleInBuild`, Schwere in `.editorconfig`). Dort zählen sie als Warnung und damit als Fehler; so gibt es je Regel eine Stelle. Vor dem Commit deshalb `dotnet format` ausführen; es behebt Leerraum und, soweit es geht, Stil. Die Pipeline prüft den Leerraum, den Rest der Build. Der Stop-Hook im Projekt (seed-template) prüft dasselbe wie die Pipeline. Die Meldung nennt je Datei die Zahl der Stellen und die erste abweichende Zeile, bis zu 20 Dateien. Dateien mit `generated_code = true` in `.editorconfig` oder dem Kopf `<auto-generated>` prüft `dotnet format` nicht.

**Offene TODOs** („Offene TODOs in project.yaml“): eine Warnung je Zeile mit `TODO(onboarding)` in `project.yaml` (auch `TODO (onboarding)`), mit Zeilennummer. So markiert ein Projekt Angaben, die beim Onboarding noch fehlen. Den Punkt erledigen und die Markierung entfernen.

**Lokal**, im Checkout des Projekts, mit einem Klon von seed-pipelines daneben, Python mit PyYAML und npm:

```bash
dotnet restore api/Api.slnx
dotnet format api/Api.slnx                               # vor dem Commit: Leerraum und Stil
dotnet build api/Api.slnx -c Release --no-restore -warnaserror -warnNotAsError:NU1603,NU1608,NU1801,NU1900,NU1901,NU1902,NU1903,NU1904,NU1905,MSB3026
python3 ../seed-pipelines/scripts/dotnet/checks.py todos --project-yaml project.yaml
python3 ../seed-pipelines/scripts/dotnet/checks.py suppressions
python3 ../seed-pipelines/scripts/dotnet/checks.py format --solution api/Api.slnx
# Mit derselben --state-file urteilt die npm-Prüfung wie in der Pipeline über NuGet und npm, ob
# Ausnahmen nichts mehr treffen; ohne urteilt jede Prüfung nur für sich.
python3 ../seed-pipelines/scripts/dotnet/checks.py packages --solution api/Api.slnx --project-yaml project.yaml --state-file /tmp/seed-package-checks.json
python3 ../seed-pipelines/scripts/dotnet/checks.py npm --project-yaml project.yaml --state-file /tmp/seed-package-checks.json
```

Nur die Lücken eines npm-Projekts zeigt auch `npm audit --omit=dev --package-lock-only` in dessen Ordner.

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
  -ExpectBuildSteps 'seed-pipelines auschecken (Skripte der Prüfungen)', 'Projekt auschecken', `
    '.NET SDK laut global.json', 'Anmeldung am Feed (Beispiel)', 'dotnet restore', `
    'npm run build', 'Lint (Beispiel)', 'Datei ins Frontend (Beispiel)', 'Artefakt web' `
  -AbsentBuildSteps 'npm ci (Erweiterung)', 'Manifest der Erweiterung prüfen', 'Artefakt extension'
# Mit Browser-Erweiterung: ihr Build nach dem Frontend und vor den eigenen Schritten
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extension.yml `
  -ExpectBuildSteps 'Node 24.x', 'npm-Pakete prüfen', 'dotnet build', 'npm ci', 'npm run build', `
    'npm ci (Erweiterung)', 'npm test (Erweiterung)', 'npm run build (Erweiterung)', 'Manifest der Erweiterung prüfen', `
    'Lint (Beispiel)', 'Artefakt web', 'Artefakt extension'
# Prüflauf wie im PR: Scans, Prüfungen im Build und eigene Schritte ja, Artefakte nein; Warnungen als Fehler
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/validation-only.yml `
  -ExpectBuildSteps 'seed-pipelines auschecken (Skripte der Prüfungen)', 'Projekt auschecken', `
    'Secret-Scan (gitleaks 8.30.1)', 'Seed-Versionen prüfen', 'terraform fmt und validate', `
    'Terraform-Sicherheitsscan (Trivy 0.76.1)', 'Anmeldung am Feed (Beispiel)', 'dotnet restore', `
    'Offene TODOs in project.yaml', 'Abgeschaltete Regeln prüfen', 'Formatierung prüfen', 'NuGet-Pakete prüfen', `
    'Node 24.x', 'npm-Pakete prüfen', 'dotnet build', 'dotnet test', 'npm ci', 'npm run build', 'Lint (Beispiel)' `
  -AbsentBuildSteps 'Artefakt web', 'Artefakt api' -ExpectText 'warnaserror -warnNotAsError:NU1603', `
    'seed-package-checks.json'
# Ohne Frontend und Erweiterung: npm-Prüfung mit dem npm des Agents, kein Node, kein npm ci
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/api-only.yml `
  -ExpectBuildSteps 'NuGet-Pakete prüfen', 'npm-Pakete prüfen', 'dotnet build', 'dotnet test' `
  -AbsentBuildSteps 'Node 24.x', 'npm ci', 'npm ci (Erweiterung)'
# Erweiterung ohne Frontend: Node genau einmal, vor der npm-Prüfung
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/extension-only.yml `
  -ExpectBuildSteps 'NuGet-Pakete prüfen', 'Node 24.x', 'npm-Pakete prüfen', 'dotnet build', 'npm ci (Erweiterung)', `
    'Manifest der Erweiterung prüfen' `
  -AbsentBuildSteps 'npm ci', 'npm run build', 'Artefakt extension'
# Ohne Warnungen als Fehler (treatWarningsAsErrors: false), die übrigen Prüfungen bleiben
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/warnings-allowed.yml `
  -ExpectBuildSteps 'dotnet restore', 'NuGet-Pakete prüfen', 'npm-Pakete prüfen', 'dotnet build', 'dotnet test' `
  -AbsentText 'warnaserror'
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
# Prüfung des Custom Connectors: nach terraform plan der Job connector_check, vor jeder Freigabe
./tests/preview/Invoke-SeedPipelinePreview.ps1 @common -YamlPath tests/preview/custom-connector.yml `
  -TemplateParameters @{ allowBreakingConnectorChanges = 'true' } `
  -ExpectPlanSteps 'terraform plan', 'Custom Connector erkennen', 'Outputs als Artefakt', `
    'seed-pipelines auschecken (Skript der Connector-Prüfung)', 'Artefakt openapi laden', 'Custom Connector prüfen' `
  -ExpectDeploySteps 'Custom Connector'
```

- `-SeedRef` ersetzt die `ref` der Repository-Ressource `seed`, damit sich ein Branch von seed-pipelines vor dem Release prüfen lässt; der Branch muss dafür auf GitHub liegen.
- `-ExpectPlanSteps` und `-AbsentPlanSteps` prüfen die erste Stage `plan_<umgebung>` samt aller ihrer Jobs, `-ExpectDeploySteps` und `-AbsentDeploySteps` die erste Stage `deploy_<umgebung>`, jeweils wie die Build-Schritte. `-ExpectText` und `-AbsentText` prüfen beliebigen Text im fertigen YAML, etwa ein Argument in einem Skript. Mit `-FinalYamlPath <datei>.final.yml` statt Organisation, Projekt und YAML prüft das Skript ein gespeichertes Ergebnis erneut, ohne Azure DevOps.
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
3. Anmeldung mit Entra ID im Namen der Person, ohne Secret: Der Connector nutzt seine Managed Identity (Option der Power Platform, Preview). Redirect-URI und Federated Credential, die die Power Platform dafür vergibt, trägt der Schritt an der Client-Registrierung aus dem Modul `sso` ein; die Pipeline ist deren Besitzerin. Die Werte liest er aus der Connector-Zeile in Dataverse, die auch Administratoren der Power-Platform-Umgebung ändern können. Eingetragen werden sie deshalb nur in der Form, die die Power Platform vergibt, sonst endet der Schritt mit einem Fehler: Redirect-URI `https://global.consent.azure-apim.net/redirect/<schlüssel>`, Aussteller `https://login.microsoftonline.com/<tenant>/v2.0`, Subject `/eid1/c/pub/t/<tenant>/a/<app der Power Platform>/<region>_<schlüssel>`, Audience `api://AzureADTokenExchange`; `<schlüssel>` ist die Kennung dieses Connectors (`connectorinternalid` ohne `shared_`).
4. Mit `customConnector.shareWith: everyone` (Standard) bekommt das Standardteam der Root-Business-Unit Lesezugriff, also alle Personen der Umgebung. Ohne Freigabe sähe nur die Pipeline den Connector, denn Basic User liest Connectors nur auf Benutzerebene. Was eine Person über den Connector darf, regeln wie im Browser App-Rollen und Capabilities der API.

Voraussetzung: Die Service-Connection-Identität ist in der Power-Platform-Umgebung Anwendungsbenutzer mit der Rolle Environment Maker; das richtet das Onboarding mit `-PowerPlatformEnvironmentUrl` ein. Fehlt das, endet der Schritt mit einem Hinweis darauf. Ohne Output `custom_connector` (Feature aus oder keine Umgebung angegeben) tut er nichts; das Artefakt `openapi` gibt es trotzdem, im Zweifel mit einer Notiz statt des Dokuments.

Ob die neue Definition Flows bricht, prüft die Pipeline schon vorher, in `plan_<env>` (nächster Abschnitt).

### Prüfung des Custom Connectors

Flows rufen Operationen des Connectors über ihre `operationId` auf. Entfällt eine Operation oder ändert sie sich, merkt das sonst erst der Flow, wenn er fehlschlägt. Deshalb vergleicht der Job `connector_check` in `plan_<env>` die Definition des bestehenden Connectors in Dataverse (Tabelle `connectors`, Spalte `openapidefinition`) mit `openapi.json` aus dem Build und sucht die Flows, die den Connector nutzen (Tabelle `workflows`, Spalte `clientdata`). Er läuft nach `terraform plan`, vor jeder Freigabe und vor dem Function-Deploy. Scheitert er, endet `plan_<env>`; Apply und Deploy entfallen, am Connector und an der API ändert sich nichts.

Eine Operation abzulösen geht damit in drei Schritten: im Code als deprecated markieren (`deprecated: true` im OpenAPI-Dokument, siehe `Bfs.Seed.OpenApi`); die Pipeline nennt bei jedem Deploy die Flows, die sie noch nutzen, und deren Besitzer migrieren; dann die Operation entfernen. Nutzt sie dann noch ein aktiver Flow, bricht der Lauf ab.

| Änderung, an einer Operation, die ein Flow nutzt | Ergebnis |
| --- | --- |
| Operation entfernt | Fehler |
| neues Pflichtfeld oder neuer Pflichtparameter, den der Flow nicht setzt | Fehler |
| Typ eines Feldes, das der Flow setzt, geändert (etwa `integer` zu `string`, auch `object` zu `string`, wenn der Flow Felder darin setzt; `integer` zu `number` zählt nicht) | Fehler |
| Query- oder Pfadparameter wechselt von Zahl (oder Liste) ohne Werteliste zu Text mit Werteliste, wie beim Update auf `Bfs.Seed.OpenApi` 0.10.0 für Enums ohne `JsonStringEnumConverter` | Warnung: Seed-Functions lesen diese Parameter als Text mit `Enum.TryParse` und nehmen die Zahl weiter an; den Flow auf die Namen umstellen. Im Body bleibt es ein Fehler, wenn ein fest eingetragener Wert nicht in der Werteliste steht |
| Enum-Wert entfernt, den der Flow fest einträgt | Fehler |
| Operation deprecated | Warnung |
| Eingabefeld entfernt, das der Flow setzt | Warnung: der Wert kommt still nicht mehr an |
| Antwortfeld entfernt oder Typ geändert, das der Flow per Ausdruck liest | Warnung: die Suche in Ausdrücken ist unscharf |
| der Flow setzt den ganzen Body oder das Feld per Ausdruck | Warnung „nicht prüfbar“ statt Fehler |
| Operation entfernt oder deprecated, kein Flow nutzt sie | Hinweis im Log |

- **Fehler nur für laufende Flows:** Fehler gelten Flows mit Status aktiv (Activated) oder angehalten (Suspended). Für ausgeschaltete Flows und Entwürfe wird daraus eine Warnung.
- **Übersteuern:** Mit `allowBreakingConnectorChanges: true` werden alle Fehler der Prüfung zu Warnungen, etwa wenn der einzige betroffene Flow ohnehin abgeschaltet wird. Wie `confirmDataDeletion` reicht das Projekt den Parameter als Laufzeitparameter durch (Beispiel oben); zugelassen wird also nur in einem von Hand gestarteten Lauf. Ein Push bringt die Zulassung nicht mit.
- **Ausgabe:** Jeder Fehler und jede Warnung erscheint als `##vso[task.logissue]` mit Flow, Status, Besitzer und dem Pfad der Action in der Definition (etwa `/Try/For_each/Check/else/Create_order`). Dazu kommt eine Zusammenfassung am Lauf (Reiter „Extensions“ bzw. Summary): Connector, Umgebung und bisheriges Backend, je Operation der Stand gegenüber dem letzten Deploy (neu, geändert, deprecated, entfernt, unverändert), die Befunde, die Flows mit Status, Besitzer und Link auf Power Automate und welche Prüfungen gelaufen sind. Namen aus Dataverse kommen ohne Steuerzeichen und `##vso[` ins Log und entschärft in die Zusammenfassung.
- **Nutzung nicht prüfbar:** Liest die Pipeline-Identität Flows nicht auf Ebene Global (Rolle `Sentinel Seed Flow Reader` fehlt), sähe sie nur eigene Flows. Statt still 0 Treffer zu melden, warnt die Prüfung dann „Nutzung nicht prüfbar“; entfernte Operationen werden zu Warnungen, nicht zu Fehlern. Sie fragt die Tiefe mit `RetrieveUserPrivilegeByPrivilegeName` ab. Dieselbe Warnung gibt es für einen Flow, der den Connector einbindet, in dem die Prüfung aber keine Action mit `operationId` erkennt, etwa eine ältere `ApiConnection`-Action mit `method` und `path`; ihn prüft man von Hand.
- **Drosselung:** Antwortet Dataverse (oder Graph) mit 429 oder 502 bis 504, wiederholt die Prüfung die Anfrage bis zu zweimal, nach `Retry-After`.
- **Wann nichts geprüft wird:** ohne Output `custom_connector` im State (Feature aus, keine Umgebung angegeben oder noch nie angewendet); der Job entfällt dann samt Agent-Start. Gibt es den Connector in der Umgebung noch nicht, endet die Prüfung mit einem Hinweis. Ist der Präfix des Standard-Publishers nicht lesbar, ohne den sich der Connector nicht finden lässt, warnt sie „Connector nicht prüfbar“. Die Angaben zum Connector kommen aus dem State, also vom letzten Apply; ändert ein Lauf `customConnector.environmentUrl`, prüft er noch gegen die bisherige Umgebung.
- **Fehlt das OpenAPI-Dokument** im Build, endet die Prüfung mit dem Fehler, mit dem sonst erst der Deploy endete.
- **Skript:** [`scripts/custom-connector/Test-SeedConnectorChange.ps1`](scripts/custom-connector/Test-SeedConnectorChange.ps1) aus seed-pipelines, die Vergleichslogik in `SeedConnectorCheck.psm1`. Der Job checkt dafür seed-pipelines aus (`checkout: seed` nach `$(Pipeline.Workspace)/seed`, dieselbe `ref` wie die Templates); die Repository-Ressource muss `seed` heißen. Er ist ein eigener Job, damit der Checkout von `terraform plan` bleibt, wo er ist. Das Skript liest nur und läuft lokal mit einem `az`-Login ebenso: `./scripts/custom-connector/Test-SeedConnectorChange.ps1 -EnvironmentUrl <url> -Name <custom_connector.name> -OpenApiFile <openapi.json>`. Tests mit Testdaten: `pwsh -NoProfile -File scripts/custom-connector/SeedConnectorCheck.Tests.ps1` (läuft in der GitHub-CI dieses Repos).

**Verbindungen prüfen (Opt-in):** Der Connector meldet sich im Namen des Kontos an, mit dem eine Verbindung angelegt wurde. Fehlt diesem Konto eine App-Rolle der API, scheitert erst der Flow: Mit `assignmentRequired` bekommt es kein Token, sonst antwortet die API mit 403. Mit `checkConnectorConnections: true` listet die Prüfung zusätzlich alle Verbindungen zum Connector mit Konto und Status, ermittelt die App-Rollen des Kontos an der Unternehmensanwendung der API, auch über Gruppen, und warnt je Verbindung ohne Rolle oder mit Fehlerstatus. Genutzt ist eine Verbindung, wenn eine Connection Reference auf sie zeigt (Flows in Solutions) oder ein Flow sie direkt einbindet (Flows außerhalb von Solutions, `connectionName` in `clientdata`); ungenutzte Verbindungen sind nur ein Hinweis. Das Konto ist das angemeldete Konto der Verbindung; nennt die Verbindung keins, was bei OAuth-Verbindungen zum Custom Connector die Regel ist, die Person, die sie angelegt hat. Meist ist das dieselbe, zwingend ist es nicht. Fehlen die Rechte unten, auch nur für Konten oder Gruppen, gibt es einmal die Warnung „Verbindungen nicht prüfbar“ und keinen Fehler.

**Rechte der Pipeline-Identität:**

| Wofür | Recht | Woher |
| --- | --- | --- |
| Connector lesen | Environment Maker (eigene Connectors) | Onboarding, Schritt 9 |
| Flows aller Konten lesen | `prvReadWorkflow` auf Ebene Global, Rolle `Sentinel Seed Flow Reader` | Onboarding, Schritt 9 |
| Connection References aller Konten lesen (nur mit `checkConnectorConnections`) | `prvReadconnectionreference` auf Ebene Global, dieselbe Rolle | Onboarding, Schritt 9 |
| Verbindungen aller Konten lesen (nur mit `checkConnectorConnections`) | Admin-Scope der Power Apps API (`api.powerapps.com/.../scopes/admin/...`) | von Hand, siehe unten |
| Unternehmensanwendung, Rollenzuweisungen, Konten, Gruppen (nur mit `checkConnectorConnections`) | Microsoft Graph, Anwendungsberechtigungen mit Admin-Consent | von Hand, siehe unten |

Die Rolle `Sentinel Seed Flow Reader` hat nur diese zwei Leserechte; `prvReadflowrun` braucht die Prüfung nicht. Bestehende Umgebungen bekommen sie mit einem erneuten Lauf des Onboardings mit `-PowerPlatformEnvironmentUrl`. Mit ihr liest die Pipeline-Identität alle Flow-Definitionen der Umgebung, auch fest eingetragene Werte; in Log und Zusammenfassung erscheinen davon nur Namen, Besitzer, Status und Pfade der Actions (und ein beanstandeter Enum-Wert, gekürzt). Die Zusammenfassung sieht jede Person, die den Lauf lesen darf.

Für die Verbindungsprüfung sind die Rechte eines Service Principals **nicht in einem Lauf geprüft**; gelaufen ist sie bisher nur unter einem Benutzerkonto mit Admin-Rechten. Nach heutigem Stand:

- **Power Apps API, Admin-Scope:** Vermutlich muss die App-Registrierung der Deployment-Identität als Verwaltungsanwendung der Power Platform registriert sein (`pac admin application register --application-id <appId>` oder `New-PowerAppManagementApp`). Das gibt ihr Admin-Zugriff auf die Verwaltungs-APIs der Power Platform im ganzen Tenant, weit mehr als die Prüfung braucht. Deshalb ist die Verbindungsprüfung Opt-in.
- **Microsoft Graph:** `Application.Read.All` (Unternehmensanwendung über `servicePrincipalNames`, `appRoleAssignedTo`), `User.ReadBasic.All` (Konto zur Verbindung) und `GroupMember.Read.All` (`transitiveMemberOf` für Rollen über Gruppen). Ob `Application.ReadWrite.OwnedBy` aus dem Onboarding für die eigene Unternehmensanwendung schon reicht, ist offen.

Das Onboarding vergibt diese Rechte bewusst nicht; wer die Verbindungsprüfung will, vergibt sie von Hand und setzt `checkConnectorConnections: true` als festen Parameter im Projekt.

**Grenzen:**

- **Ausdruckssuche unscharf:** Antwortfelder erkennt die Prüfung in `body('Action')?['feld']`, `outputs('Action')?['body/feld']` und über `items('Schleife')` einer Schleife über eine Antwort, auch verschachtelt. Zugriffe über Variablen, `item()` in Select oder Filter oder Actions, die die ganze Antwort weiterreichen, sieht sie nicht; liest ein Flow ein übergeordnetes Objekt, meldet sie entfallene Unterfelder vorsichtshalber.
- **Canvas Apps** prüft sie nicht: Dataverse zeigt nur, ob eine App den Connector nutzt, nicht welche Operation.
- **Connector-Aktionen in Copilot Studio** prüft sie ebenfalls nicht, nur Cloud Flows.
- **Nur der Connector, den Seed anlegt** (Name `<Präfix>_<custom_connector.name>`); manuell oder per APIM-Export angelegte Connectoren nicht.
- Maßgeblich ist die Flow-Definition, nicht ob eine Action zur Laufzeit erreicht wird.
- **Links auf Power Automate** nutzen die `resourceid` des Flows; Entwürfe, die nie aktiv waren, haben keine und erscheinen ohne Link.

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

Statt über Edge Add-ons kann das Projekt die Erweiterung selbst ausliefern: Mit `extension.signing` signiert `deploy_<env>` das Paket als CRX3 und legt es im Daten-Storage des Projekts ab. Die API liefert es mit [`Bfs.Seed.Extension`](https://github.com/blackforestsentinel/seed-packages/tree/main/dotnet/src/Bfs.Seed.Extension) unter `/api/extension/updates.xml` aus; verwaltete Geräte installieren es von dort per Intune-Richtlinie (`ExtensionSettings`, von den Benutzern ausschaltbar) und holen Updates selbst, alle paar Stunden.

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
- **Intune:** Die Richtlinie (`ExtensionSettings`, je Erweiterung `normal_installed` mit `update_url`) pflegt die zentrale Pipeline `seed-intune` je Tenant (siehe [Intune: Edge-Erweiterungen verteilen](#intune-edge-erweiterungen-verteilen)). Nach dem ersten erfolgreichen Veröffentlichen kommt die Erweiterung per Pull Request in deren Registerdatei; `id` und `updateUrl` stehen in `release.json` (Artefakt `extension_release_<env>`). Vorher lehnt die Prüfung dort den Eintrag ab, weil `updates.xml` die ID noch nicht ausliefert. Edge installiert Erweiterungen außerhalb des Stores nur auf verwalteten Geräten.

#### Schlüssel der Erweiterung

Die Erweiterungs-ID folgt aus dem öffentlichen Schlüssel (SHA-256, die ersten 16 Byte als `a` bis `p`). [`onboarding/New-SeedExtensionKey.ps1`](onboarding/New-SeedExtensionKey.ps1) erzeugt lokal einen RSA-Schlüssel (2048 Bit, PEM, PKCS#8), zeigt die ID, lädt den Schlüssel als Secure File ins Azure-DevOps-Projekt und berechtigt die Pipeline dafür. Es läuft mit dem `az`-Login einer Person, die im Projekt Secure Files anlegen darf; `-DryRun` zeigt nur an, was fehlt.

```powershell
./onboarding/New-SeedExtensionKey.ps1 -KeyFile C:\keys\my-extension.pem `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -PipelineName <pipeline> -DryRun
```

- **Ausgabe:** die Werte für `extension.signing` und für `project.yaml` je signierter Umgebung, `auth.extraRedirectUris` (`https://<id>.chromiumapp.org/`) und `hosting.corsAllowedOrigins` (`chrome-extension://<id>`).
- **Die Datei** darf vom Anlegen an nur der eigene Benutzer lesen: unter Windows mit eigener Zugriffsliste ohne geerbte Einträge, sonst mit Modus 600.
- **Den Schlüssel sicher offline ablegen** (Passwortmanager, Tresor) und die Datei danach löschen. Azure DevOps gibt Secure Files nicht wieder heraus, und ein neuer Schlüssel ergibt eine neue ID: Browser sehen eine neue Erweiterung, Redirect-URI, CORS und die Intune-Richtlinie brauchen die neue ID.
- **Wiederholen:** Gibt es die Secure File schon, entsteht kein neuer Schlüssel. Mit `-ExistingKey -KeyFile <pem>` lädt das Skript einen vorhandenen Schlüssel hoch, falls die Secure File fehlt, und prüft ihn sonst gegen die ID, die es beim Hochladen an der Secure File vermerkt hat (Eigenschaft `extensionId`).
- **Branch-Prüfung an der Secure File:** Die Berechtigung der Pipeline hängt nicht am Branch; ohne Prüfung könnte ein PR-Lauf mit geänderter Pipeline-Datei die Secure File laden und ausgeben. Das Skript setzt deshalb den Check „Branch control“ an der Secure File: Nur Läufe von `-AllowedBranches` (Standard `main`) mit seed-pipelines aus `-TemplateRefs` (Standard `refs/tags/v*`, der Deploy checkt seed-pipelines zum Signieren aus) bekommen den Schlüssel. Eine vorhandene Prüfung mit anderen Branches wird aktualisiert. Für bestehende Secure Files das Skript mit `-ExistingKey` oder ohne erneut ausführen; es erzeugt dabei keinen neuen Schlüssel.
- Der private Schlüssel erscheint in keiner Ausgabe. Im Deploy liegt er nur für die Dauer des Jobs auf dem Agent (`DownloadSecureFile@1`).

### Voraussetzungen

- GitHub-Service-Connection in der Azure-DevOps-Organisation (Name frei wählbar, im Projekt als `endpoint` angegeben).
- Azure-Service-Connection mit Workload Identity Federation. Terraform bekommt deren OIDC-Token, es gibt keine Secrets.
- Rollen der Service-Connection-Identität: `Contributor` und `Role Based Access Control Administrator` auf der Subscription, `Storage Blob Data Contributor` auf dem State-Storage.
- Je Umgebung zwei Environments, für die Pipeline berechtigt: `<project>-<env>` mit Freigabe als Check (Infrastruktur) und `<project>-<env>-app` für den App-Deploy, dort optional eine Freigabe, etwa in Produktion. Azure DevOps legt sie nicht selbst an.

### Eigene Agents

Mit `pool: <Name>` laufen alle Jobs im angegebenen Agent-Pool statt auf Microsoft-gehosteten Agents (`vmImage`). Das gilt ebenso für `templates/scaffold.yml` und `templates/intune-extensions.yml`. Gedacht ist das für Organisationen ohne gehostete Minuten oder für Agents im eigenen Netz.

- **Was die Agents brauchen:** Linux (x64) mit bash, curl, zip, unzip, tar, git, python3 mit PyYAML und der Azure CLI. PyYAML (Paket `python3-yaml`) haben Ubuntu-Server, gehostete Agents und Ubuntu unter WSL schon, als Abhängigkeit von netplan und cloud-init; sonst `apt install python3-yaml`. Die Paketprüfung im Build liest damit `project.yaml` (siehe Prüfungen im Build). Für `features.customConnector` und das zentrale Scaffold mit `migrate` oder `enableBranchPolicy` kommt PowerShell 7.2 oder neuer (`pwsh`) dazu, für `extension.signing` openssl. Den Deploy des Frontends (`AzureStaticWebApp@0`) führt der Task in einem Container aus; dafür braucht der Agent einen `docker`-Befehl. Am besten rootless, etwa Podman mit `podman-docker`: Ein Agent-Benutzer in der Gruppe `docker` hätte über den Daemon Root-Rechte auf der Maschine.
- **Was die Pipeline selbst lädt:** das .NET SDK laut `global.json`, Node in `nodeVersion` (mit Frontend oder `extension`), Terraform, gitleaks und Trivy. Terraform, gitleaks und Trivy prüft sie per SHA-256 und lädt sie in jedem Job neu, statt einem vorhandenen zu trauen (siehe Terraform und Provider). Der Hinweis auf veraltetes Seed fragt github.com, api.nuget.org und registry.npmjs.org ab; ohne diese Ziele entfällt er still. Die Paketprüfungen fragen nuget.org und die npm-Registry aus `.npmrc`; ohne Netz warnen sie. Ohne Frontend und Erweiterung nimmt „npm-Pakete prüfen“ das npm des Agents und warnt, wenn es fehlt, obwohl das Repo npm-Projekte hat (siehe Prüfungen im Build).
- **Anmeldung:** Die Azure CLI meldet sich in jedem Schritt mit einem eigenen Konfigurationsverzeichnis an (`AZURE_CONFIG_DIR`); eine Anmeldung des Agent-Benutzers bleibt unberührt.
- **Reihenfolge:** Mit einem einzigen Agent laufen Stages und Läufe nacheinander, wie mit einem gehosteten parallelen Job.
- **Berechtigung:** Der Pool muss im Projekt für die Pipeline berechtigt sein, sonst fragt der erste Lauf danach („Permit“).
- **Abschottung:** Ein eigener Agent läuft dauerhaft und für jede Pipeline, die seine Queue nutzen darf. Was ein Lauf dort hinterlässt (Dateien im Profil, Werkzeuge im Tool-Verzeichnis), trifft spätere Läufe, auch die mit der Service Connection. Deshalb:
  - ein eigener Pool statt `Default`, ohne „Auto-provision this agent pool in all projects“, nur in den Projekten, die ihn brauchen; `Default` kommt in jedes neue Projekt;
  - die Queue nur für einzelne Pipelines freigeben und an ihr unter „Approvals and checks“ eine Branch-Prüfung (nur `main`) oder eine Freigabe setzen; `Initialize-SeedProject.ps1 -Pool` warnt, wenn das fehlt;
  - der Agent-Benutzer ohne Administratorrechte und ohne Zugriff auf andere Konten oder Anmeldedaten der Maschine, etwa auf einer eigenen VM oder in einer eigenen WSL-Distribution mit `[interop] enabled=false` und `[automount] enabled=false` in `/etc/wsl.conf`, sonst erreicht jeder Lauf das Windows-Profil und startet Windows-Programme.

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
git clone --branch v0.19.0 https://github.com/blackforestsentinel/seed-pipelines.git
cd seed-pipelines
./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -Approvers it@example.org -DryRun
```

1. Deployment-Identität: App-Registrierung ohne Secret
2. Azure: `Contributor` und `Role Based Access Control Administrator` auf der Subscription. Die Bedingung erlaubt nur die Rollen, die Seed-Module vergeben: Storage-Datenrollen, `Key Vault Secrets User` und `Key Vault Secrets Officer`, `Monitoring Metrics Publisher`. Nach einem Seed-Update mit neuen Rollen das Skript erneut ausführen; es ersetzt dann die ältere Bedingung. Dazu kommt die eigene Rolle `Sentinel Seed Lock Contributor`, die nur Löschsperren lesen, setzen und entfernen darf (Modul `storage`); Contributor allein darf das nicht.
3. Microsoft Graph: `Application.ReadWrite.OwnedBy` mit Admin-Consent (Modul `sso`)
4. Terraform-State: Storage Account ohne Shared Key, `Storage Blob Data Contributor` nur auf dem Container
5. Service Connection per Workload Identity Federation, mit Branch-Prüfung und Required template (siehe unten)
6. GitHub-Service-Connection (Token aus `$env:SEED_GITHUB_TOKEN`, sonst Hinweis)
7. Repo und Pipeline `seed-scaffold` (entfällt mit `-SkipScaffold`)
8. GitHub-Service-Connection für alle Pipelines
9. Nur mit `-PowerPlatformEnvironmentUrl <url>[,<url>]`: die Deployment-Identität als Anwendungsbenutzer mit der Rolle Environment Maker in jeder genannten Power-Platform-Umgebung (Umgebungs-URL aus dem Power Platform Admin Center, die Umgebung braucht Dataverse), für den Custom Connector. Dazu je Umgebung die eigene Sicherheitsrolle `Sentinel Seed Flow Reader` in der Root-Business-Unit, nur mit `prvReadWorkflow` und `prvReadconnectionreference` auf Ebene Global, für die [Prüfung des Custom Connectors](#prüfung-des-custom-connectors): Environment Maker liest Flows nur auf Ebene Basic, also eigene. Fehlt der Rolle ein Recht oder hat es eine kleinere Tiefe, ergänzt das Skript es; weitere Rechte, die jemand ergänzt hat, bleiben. Das Konto braucht dort System Administrator. Weitere Umgebungen später: Skript erneut mit allen URLs ausführen; bestehende Umgebungen bekommen die Rolle ebenso.

**Weitere Subscription im selben Tenant:** Das Skript erneut ausführen, mit eigener Identität und Service Connection (`-IdentityName`, `-ServiceConnectionName`). Der State-Account bekommt nach Default je Subscription einen eigenen Namen. Die Rolle `Sentinel Seed Lock Contributor` gibt es im Tenant nur einmal: Das Skript sucht sie in allen Subscriptions, die das Konto sieht, und nimmt die neue Subscription in ihre `assignableScopes` auf (im DryRun `[alt]`). Dafür braucht das Konto auch in den Subscriptions, in denen die Rolle schon zuweisbar ist, Owner oder User Access Administrator. Bestehende Identitäten fasst das Skript nicht an.

**Branch-Prüfung:** An der Azure-Service-Connection hängt der Check „Branch control“: Nur Läufe von `-AllowedBranches` (Standard `main`, Muster wie `release/*` erlaubt) bekommen die Deployment-Identität, PR-Läufe und Feature-Branches nicht. Freigaben allein reichen dafür nicht, denn sie hängen an den Environments: Eine geänderte Pipeline-Datei auf einem Feature-Branch, die die Service Connection direkt nutzt, käme ohne die Prüfung an der Freigabe vorbei und hätte die Rechte der Identität auf der ganzen Subscription. Ein erneuter Lauf mit anderen Branches aktualisiert die Prüfung.

Die Prüfung vergleicht **jedes** Repo des Laufs mit derselben Liste, auch `seed-pipelines`, das Projekte über einen Tag einbinden. Deshalb erlaubt sie zusätzlich `-TemplateRefs` (Standard `refs/tags/v*`); ohne sie scheitert jeder Lauf auf `main`. Die Kehrseite: Ein Tag `v…` im Projekt-Repo und ein Lauf darauf kämen ebenfalls durch, ohne Review und ohne Freigabe. Deshalb dürfen dort nur Administratoren Tags anlegen: Das Onboarding setzt „Create tag“ auf Deny für Contributors, Build Administrators, `<Projekt> Build Service` und `Project Collection Build Service`, also auch für die Job-Tokens der Pipelines. `Initialize-SeedTenant.ps1` tut das für das ganze Azure-DevOps-Projekt (nicht mit `-SkipScaffold`), `Initialize-SeedProject.ps1` für das Repo des Projekts, `Grant-SeedScaffoldAccess.ps1` für ein Zielprojekt des zentralen Scaffolds. Brauchen andere Repos im selben Projekt Tags, lässt `-AllowTagCreation` das projektweite Verbot weg; das Skript warnt dann. Ein Deny gilt auch für Administratoren, die zugleich Contributors sind; Seed-Pipelines legen selbst keine Tags an. Wer Seed-Änderungen in einem Testprojekt vor dem Release prüft, nimmt die Branches von seed-pipelines in `-TemplateRefs` auf, etwa `refs/heads/feat/*`.

Das Verbot trifft auch jede weitere Identität, der „Create tag“ in den Zugriffslisten ausdrücklich erlaubt ist: Nach den vier Denies liest das Skript die Liste des Projekts und seiner Repos (`Initialize-SeedProject.ps1`: des Projekts und des Repos) und verbietet das Recht dort, wo es erlaubt ist, etwa der Person, die ein Repo angelegt hat (sie bekommt das Recht beim Anlegen ausdrücklich), oder einer eigenen Gruppe. Die übrigen Rechte des Eintrags bleiben. Ausgenommen sind Project Administrators, Project Collection Administrators und Project Collection Service Accounts. `-DryRun` nennt die Identitäten nur. Ein Deny für eine Person gilt auch, wenn sie Administratorin ist; Tags legt sie dann nach einer Änderung der Rechte von Hand an.

**Required template:** Die Branch-Prüfung lässt jede Pipeline-Datei auf `main` an die Service Connection, auch eine, die sie direkt nutzt statt über `web-app.yml` (etwa ein `AzureCLI@2`, der das Token ausgibt). Deshalb hängt an der Azure-Service-Connection zusätzlich die Prüfung „Required template“ (ExtendsCheck): Nur Läufe, die `templates/web-app.yml` aus `blackforestsentinel/seed-pipelines` an einem erlaubten Tag per `extends` einbinden, bekommen die Deployment-Identität. Erlaubt sind ohne weitere Angabe `-SeedPipelinesVersion` und die Tags, die die für die Service Connection berechtigten Pipelines auf ihrem Default-Branch einbinden; vorhandene Einträge bleiben. `seed-scaffold` (PAT) nutzt dieselbe Service Connection und bekommt `templates/scaffold.yml` am selben Tag und an dem Tag, den sein `azure-pipelines.yml` nennt. `-RequiredTemplateRefs v0.19.0[,v0.18.1]` ersetzt die Einträge für `web-app.yml` durch genau diese Tags, etwa um alte zu entfernen; das Skript warnt, wenn eine berechtigte Pipeline dann scheitern würde. `-SkipRequiredTemplate` lässt die Prüfung weg, etwa wenn die Service Connection auch Pipelines außerhalb von Seed dient; das Skript warnt dann. Branches von seed-pipelines nimmt die Prüfung nie von selbst auf; wer vor dem Release in einem Testprojekt prüft, nennt sie in `-RequiredTemplateRefs` (etwa `refs/heads/feat/x`) und entfernt sie danach wieder.

**Versionswechsel mit Required template:** Die Prüfung kennt nur die Tags, die sie nennt. Vor dem Merge eines Pull Requests, der `ref` eines Projekts auf einen neuen Tag hebt, den Tag deshalb ergänzen, sonst scheitert der erste Lauf auf `main` an der Prüfung (PR-Läufe nicht, sie nutzen die Service Connection nicht):

```powershell
# ergänzt v0.19.0, vorhandene Tags bleiben
./onboarding/Initialize-SeedTenant.ps1 <Parameter wie beim Onboarding> -SeedPipelinesVersion v0.19.0 -DryRun
# bzw. für ein migriertes Projekt
./onboarding/Initialize-SeedProject.ps1 <Parameter wie beim Einrichten> -RequiredTemplateRefs v0.18.1,v0.19.0 -DryRun
```

Ohne `-DryRun` wiederholen, dann mergen. Sind alle Projekte umgestellt, entfernt ein Lauf mit `-RequiredTemplateRefs <neuer Tag>` die alten. Von Hand geht es auch: Service Connection, „Approvals and checks“, „Required template“, Eintrag mit dem neuen Ref ergänzen.

Mit `-SkipScaffold` setzt das Skript sie nicht, weil eine Migration vor dem Umschalten auf einem eigenen Branch testet; das übernimmt `Initialize-SeedProject.ps1`, sobald das Projekt umgestellt ist. Für bestehende Installationen genügt es, `Initialize-SeedProject.ps1` (nach dem Umschalten) bzw. `Initialize-SeedTenant.ps1` erneut auszuführen.

`-SkipScaffold` lässt Repo und Pipeline `seed-scaffold` weg, für Projekte, die migriert statt neu angelegt werden (siehe [Bestehendes Projekt migrieren](#bestehendes-projekt-migrieren)); der PAT entfällt dann.

`-CentralScaffold` richtet `seed-scaffold` als zentrales Scaffold für andere Azure-DevOps-Projekte ein, ohne PAT (siehe [Zentrales Scaffold](#zentrales-scaffold-selbstbedienung-über-projektgrenzen)).

`-Pool <Name>` lässt `seed-scaffold` auf eigenen Agents laufen (siehe [Eigene Agents](#eigene-agents)); ohne den Parameter nutzt es Microsoft-gehostete Agents. Mit `-CentralScaffold` warnt das Skript dabei.

Danach einmalig einen PAT anlegen und als geheime Variable `SeedScaffoldPat` an der Pipeline `seed-scaffold` hinterlegen. Scopes: Code (Read, write & manage), Build (Read & execute), Environment (Read & manage), Graph (Read), Project and Team (Read), Pipeline Resources (Use and manage). Das Job-Token der Pipeline reicht nicht, solange „Protect access to repositories in YAML pipelines“ an ist; dann darf es keine Repos anlegen. Die Person, deren PAT es ist, braucht außerdem „Edit policies“ an den Repos des Projekts (Branch-Policy der PR-Validierung).

## templates/scaffold.yml: neues Projekt anlegen

Die Pipeline `seed-scaffold` (Vorlage: [`scaffold/azure-pipelines.yml`](scaffold/azure-pipelines.yml)) fragt beim Start Name, `sso`, `mcp`, `storage`, Frontend, Umgebungen und Freigebende ab und legt über das Terraform-Modul `seed-terraform//ado-project` an:

- Repo aus `seed-template` mit `project.yaml` und `azure-pipelines.yml` (ohne Frontend: `hosting.staticWebApp: none` und `frontend: false`)
- Environments `<name>-<env>` (Freigabe für Infrastruktur) und `<name>-<env>-app`
- Pipeline, berechtigt für die Environments; auf Wunsch startet sie den ersten Lauf
- Branch-Policy auf `main`: Pull Requests brauchen einen erfolgreichen PR-Lauf dieser Pipeline (siehe PR-Validierung). Die Policy ist Pflicht und sperrt damit direkte Pushes auf `main`.

Die Azure-Service-Connection berechtigt `seed-scaffold` bewusst nicht selbst: Im ersten Lauf eines neuen Projekts gibt eine Administratorin oder ein Administrator sie einmal frei („Permit“). PR-Läufe brauchen keine Freigabe, weil sie die Service Connection nicht verwenden. Der State liegt unter `scaffold/<name>.tfstate`; ein erneuter Lauf ergänzt nur, was fehlt.

Für die Branch-Policy reicht der Scope Code (Read, write & manage) des PATs; die Person, deren PAT es ist, braucht am Repo die Berechtigung „Edit policies“ (Projektadministratoren haben sie). Die Policy setzt voraus, dass das neue Projekt seed-pipelines ab v0.5.0 einbindet (`pipelinesVersion`); ältere Versionen würden in PR-Läufen deployen, `ado-project` bricht dann ab.

Optional, auch ohne zentrales Scaffold: `approverGroups` (Gruppen der Organisation als Freigebende), `requesterCanApprove`, `minReviewers` (Pflicht-Reviewer auf `main`) und `projectPool` (eigener Agent-Pool des neuen Projekts).

## Zentrales Scaffold: Selbstbedienung über Projektgrenzen

Ein zentrales Azure-DevOps-Projekt legt Seed-Projekte in anderen Projekten der Organisation an, etwa wenn jedes Vorhaben ein eigenes Projekt hat. Wer ein Projekt anlegt, braucht im Zielprojekt nur Contributors-Rechte und im zentralen Projekt Readers und „Queue builds“ an `seed-scaffold`. Dafür braucht es weder Admin-Rechte noch einen PAT noch eine zusätzliche Lizenz.

Vorlage: [`scaffold/central-pipelines.yml`](scaffold/central-pipelines.yml), `templates/scaffold.yml` mit `azureDevOpsAuth: jobToken`.

### Wie es funktioniert

- **Anmeldung:** mit dem Job-Token. Dahinter steht die Build-Service-Identität des zentralen Projekts, „<Projekt> Build Service (<Organisation>)“. Sie entsteht mit dem Projekt und zählt nicht als Benutzer mit Zugriffsstufe. Ein eigener Service Principal bräuchte für Repos die Zugriffsstufe Basic, die je Organisation kostet.
- **Freischalten:** Ein Projektadmin schaltet jedes Zielprojekt einmal frei (`onboarding/Grant-SeedScaffoldAccess.ps1`). Ein nicht freigeschaltetes Projekt sieht das Scaffold gar nicht.
- **Repos:** Die Identität legt keine Repos an. Unter „Protect access to repositories in YAML pipelines“ sieht ihr Token nur Repos, die der Lauf auscheckt. Bei `new` legt die Person das Repo vorher an, mit README.
  - Das Scaffold referenziert es per Inline-Checkout (`git://<projekt>/<repo>`); in `resources.repositories` sind Template-Ausdrücke nicht erlaubt. Die Referenz gibt dem Token schon ab Start des Jobs Zugriff; ausgecheckt wird das Repo (mit `persistCredentials`) aber erst nach den Prüfungen unten.
  - seed-template führt es per Merge unabhängiger Historien ein; bei gleichen Dateien wie `README.md` gewinnt seed-template.
- **Service Connections:**
  - Das Scaffold teilt die Azure- und die GitHub-Service-Connection des zentralen Projekts ins Zielprojekt, unter demselben Namen.
  - Wer teilt, wird dort Administrator der Service Connection; so berechtigt das Scaffold die neue Pipeline selbst, und der erste Lauf braucht kein „Permit“.
  - Bei Workload Identity Federation hängt der Subject an der ID der Service Connection (`…/sc/<org-id>/<sc-id>`), nicht am Projekt. Ein weiterer Federated Credential ist deshalb nicht nötig.
- **Branch-Prüfung:** Checks an einer geteilten Service Connection gelten in allen Projekten, und Projektadmins der Zielprojekte können sie nicht ändern. Das Onboarding setzt sie einmal im zentralen Projekt; das Scaffold fasst sie nicht an.
- **Prüfungen vor jeder Änderung** (`scripts/scaffold/scaffold.py`, Tests in `test_scaffold.py`):
  - die Eingaben;
  - das Zielprojekt (`allowedProjects`);
  - die Mitgliedschaft der startenden Person in `memberGroup` (Standard Contributors, auch über Teams und Gruppen);
  - das Repo: bei `new` Default-Branch `main` und kein fremdes `project.yaml`;
  - die Namen der Service Connections im Zielprojekt;
  - ob der Projektname frei ist: kein Terraform-State unter `<name>/`, kein `scaffold/<name>.tfstate`, keine Resource Group mit Tag `seed:project=<name>`. Freie Namen trägt das Scaffold atomar in die Registry ein (`scaffold/registry/<name>.json`, `If-None-Match: *`). Ein erneuter Lauf für dasselbe Zielprojekt ist in Ordnung.
- **Eingaben in Skripten:** Eingaben der startenden Person erreichen Skripte nur über die Umgebung.
- **Freigabe und Review:**
  - `approverGroups`: eine feste Gruppe der Organisation als Freigebende;
  - `requesterCanApprove: false`, fest in der Vorlage: Niemand gibt seine eigene Infrastruktur allein frei. Ohne `approverGroups` bestimmt die startende Person die Freigebenden selbst (`approvers`), und ohne Eintrag hat das neue Projekt keine Freigabe; das Onboarding warnt dann. In bestehenden `seed-scaffold`-Repos den Wert in `azure-pipelines.yml` per Hand setzen; das Onboarding legt die Datei nur einmal an;
  - `minReviewers` (Standard 1 im Onboarding): Pull Requests auf `main` brauchen eine Zustimmung, die eigene zählt nicht. Die Stage Plan nutzt die Service Connection schon vor jeder Freigabe; ohne Review könnte eine Person ihre eigene Pipeline-Änderung mergen.
- **State:** unter `scaffold/<projekt-id>/<name>.tfstate`. Eigene Azure-Identität des Scaffolds: Managed Identity mit Storage Blob Data Contributor auf dem State-Container und Reader auf der Subscription. Die Deployment-Identität teilt das Scaffold nur weiter, es nutzt sie nicht selbst.
  - Im selben Container liegen State und Pläne aller Projekte, mit sensiblen Werten wie den Deployment-Tokens der Static Web Apps. Eine Bedingung an der Rolle (ABAC, `blobs:path StringStartsWith 'scaffold/'`) lässt die Identität deshalb nur Blobs unter `scaffold/` lesen, schreiben und löschen: die Registry und ihren eigenen State.
  - Auflisten darf sie den ganzen Container: Die Namensprüfung sucht State unter `<name>/`, und eine Liste nennt nur Namen.
  - Bestehende Installationen: `Initialize-SeedTenant.ps1 -CentralScaffold` erneut ausführen; es ergänzt die Bedingung an der vorhandenen Zuweisung (im DryRun `[alt]`).

### Einrichten

1. **Onboarding im zentralen Projekt**, mit denselben Rechten wie sonst:
   ```powershell
   ./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> -AzureDevOpsOrganization https://dev.azure.com/<org> `
       -AzureDevOpsProject Sentinel-Seed -CentralScaffold -AllowedProjects '^Apps-.+$' `
       -ApproverGroups 'Seed Infrastruktur-Freigabe' -DryRun
   ```
   `-Pool` und `-ProjectPool` für eigene Agents. Das Skript warnt, ohne etwas zu ändern, bei Einstellungen aus Punkt 2, die das Job-Token über das Scaffold hinaus öffnen, ohne `-ApproverGroups` und mit `-Pool`; nach Punkt 2 zeigt ein erneuter Lauf mit `-DryRun`, ob noch etwas offen ist.
2. **Im zentralen Projekt per UI:**
   - Entwicklerinnen und Entwickler als Readers aufnehmen;
   - unter Pipelines, `seed-scaffold`, Manage security den Readers „Queue builds“ geben;
   - das Repo `seed-scaffold` und Pipelines ändern nur Admins, denn jede Pipeline des Projekts bekäme dasselbe Token. Deshalb keine weiteren Pipelines im Projekt und am Repo `seed-scaffold` eine Branch-Policy mit Pflicht-Reviewer;
   - unter Project settings, Pipelines, Settings: „Limit job authorization scope to current project for non-release pipelines“, „Protect access to repositories in YAML pipelines“, „Limit variables that can be set at queue time“ und „Disable creation of classic build pipelines“ bzw. „… classic release pipelines“ an. Sonst liefe das Token als Project Collection Build Service, sähe alle Repos der Zielprojekte, oder wer startet, setzte beliebige Umgebungsvariablen.
3. **Je Zielprojekt einmal, durch einen Projektadmin:**
   ```powershell
   ./onboarding/Grant-SeedScaffoldAccess.ps1 -AzureDevOpsOrganization https://dev.azure.com/<org> `
       -AzureDevOpsProject <ziel> -CentralProject Sentinel-Seed -AllowContributorsToCreateRepositories -Check
   ```
   Ohne `-Check` vergibt das Skript die Rechte (Skript-Hilfe). Mit `-AgentPool` gilt das auch für einen eigenen Pool der neuen Projekte.
4. **Agents:** `migrate` und `enableBranchPolicy` rufen `Initialize-SeedProject.ps1` auf und brauchen PowerShell 7; Microsoft-gehostete Agents haben es. Für das zentrale Scaffold sind sie auch sonst die bessere Wahl: Sein Job-Token erreicht alle freigeschalteten Projekte, und auf einem eigenen Agent träfe, was ein Lauf hinterlässt, die folgenden.

### Ein Projekt anlegen oder einrichten

- **`new`:** Im Zielprojekt unter Repos, New repository das Repo mit README anlegen. Dann `seed-scaffold` starten, mit Zielprojekt, Repo, Projektname, Features, Umgebungen und Modus `new`. Der erste Lauf des neuen Projekts startet und wartet auf die Freigabe der Infrastruktur.
- **`migrate`:** vorhandenes Repo einrichten wie mit `Initialize-SeedProject.ps1`, ohne Dateien zu schreiben. Danach stellt das Projekt `azure-pipelines.yml` und `project.yaml` per Pull Request um; anschließend folgt **`enableBranchPolicy`** für die Branch-Policies. Ist `main` beim Lauf mit `migrate` schon umgestellt, setzt bereits `migrate` sie, wie das Skript es standardmäßig tut.
- **`dryRun`:** nur prüfen, nichts ändern.

### Grenzen

- **Reichweite der Deployment-Identität:** Alle Zielprojekte teilen sich die Deployment-Identität. Projektadmins eines Zielprojekts können eigene Pipelines für die geteilte Service Connection berechtigen und mit `main` eines eigenen Repos an der Branch-Prüfung vorbei. Pflicht-Reviewer schützen vor Contributors, nicht vor Projektadmins. Abhilfe wäre eine Identität je Zielprojekt mit Rechten nur auf dessen Resource Groups.
- **Migration:** Weil die Branch-Prüfung global gilt, testet eine Migration vor dem Umschalten nicht mehr auf einem eigenen Branch mit der Service Connection, sondern per PR-Lauf.
- **Admin-Consent:** Features mit Graph-Berechtigungen (`auth.directory`, `mcp`) brauchen weiter einmal Admin-Consent. Neue Power-Platform-Umgebungen brauchen das Onboarding mit `-PowerPlatformEnvironmentUrl`.

## Bestehendes Projekt migrieren

Ein Projekt mit eigenem Repo und eigenen Pipelines zieht ohne neues Repo auf Seed um. [`onboarding/Initialize-SeedProject.ps1`](onboarding/Initialize-SeedProject.ps1) legt dafür an, was sonst `seed-terraform//ado-project` für neue Projekte anlegt, mit denselben Einstellungen, aber ohne Repo-Import und ohne Dateien zu schreiben:

- Environments `<name>-<env>` mit Freigabe und `<name>-<env>-app`, dort mit Freigabe nur für `-AppApprovalEnvironments`. Wer einen Lauf gestartet hat, gibt ihn nicht selbst frei; `-RequesterCanApprove` erlaubt das ausdrücklich. Ein erneuter Lauf gleicht vorhandene Freigaben an (bis v0.18 war es umgekehrt: Freigabe auch durch die startende Person, außer mit `-RequesterCannotApprove`; der Schalter bleibt ohne Wirkung erhalten)
- die Pipeline-Definition für die YAML-Datei (Default `azure-pipelines.yml`), ohne sie zu starten
- die Berechtigungen der Pipeline für die Environments, die Azure- und die GitHub-Service-Connection; das „Permit“ beim ersten Lauf entfällt
- nach dem Umschalten den Branch-Schutz: die Branch-Policy der PR-Validierung, einen Pflicht-Reviewer (`-MinReviewers`, Standard 1, die eigene Stimme zählt nicht), die Branch-Prüfung an der Azure-Service-Connection (`-AllowedBranches`, Standard der Default-Branch) und dort die Prüfung „Required template“ mit dem Tag, den die YAML-Datei auf dem Default-Branch einbindet, und den Tags der übrigen berechtigten Pipelines (`-RequiredTemplateRefs`, `-SkipRequiredTemplate`, siehe [Required template](#tenant-onboarding)); siehe unten, PR-Validierung als Standard
- Tags im Repo nur für Administratoren, auch für Identitäten, denen „Create tag“ im Projekt oder Repo ausdrücklich erlaubt ist (siehe [Branch-Prüfung](#tenant-onboarding)); `-AllowTagCreation` lässt das mit Warnung weg
- mit `-Pool` Warnungen, wenn die Agent-Queue ohne Freigabe oder Branch-Prüfung ist, allen Pipelines offensteht oder der Pool in jedes neue Projekt kommt (siehe [Eigene Agents](#eigene-agents))

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
     -Environments test,prod -Approvers it@example.org -AppApprovalEnvironments prod `
     -ServiceConnectionName sc-<name> -DryRun
   ```

3. **Repo umstellen** auf einem eigenen Branch: `project.yaml`, `azure-pipelines.yml` mit `extends` auf `web-app.yml` (siehe oben) und die Projektstruktur, die das Template erwartet.
4. **Pipeline auf dem Branch** von Hand starten (`az pipelines run --name <pipeline> --branch <branch>`) und Plan, Deploy und Smoke-Test prüfen. Ein Lauf auf einem Branch deployt wie einer auf `main`, mit denselben Freigaben; wer zuerst nur die erste Umgebung prüfen will, lehnt die Freigabe der nächsten ab.
5. **Alte Pipelines deaktivieren**, bevor der Branch nach `main` geht (Pipeline-Einstellungen, „Disabled“). Sonst startet der Merge sie mit: eine alte Pipeline auf derselben YAML-Datei mit dem neuen Inhalt, andere mit ihren alten Dateien, solange es diese noch gibt.
6. **Mergen.** Ab dann deployt die Seed-Pipeline bei jedem Push auf `main`.
7. **Branch-Schutz nach dem Umschalten:** `Initialize-SeedProject.ps1` mit denselben Parametern erneut ausführen. Bindet die YAML-Datei auf `main` jetzt `web-app.yml` ab v0.5.0 ein, setzt das Skript Branch-Policy, Pflicht-Reviewer und Branch-Prüfung von selbst. Danach gehen Änderungen an `main` nur noch per PR, und nur Läufe von `main` bekommen die Azure-Service-Connection.

Zwischen Schritt 2 und 6 liegt auf `main` noch die alte Datei. Hat sie denselben Pfad, startet ein Push auf `main` auch die neue Pipeline, mit dem alten Inhalt. Diese Pipeline ist für die alten Service Connections und Environments nicht berechtigt und fragt nach Zugriff: solche Läufe abbrechen, den Zugriff nicht erlauben. `Initialize-SeedProject.ps1` weist darauf hin und listet die übrigen aktiven Pipelines des Repos.

### PR-Validierung als Standard

Den Branch-Schutz (Branch-Policy mit Build-Validierung durch die Seed-Pipeline, Pflicht-Reviewer, Branch-Prüfung und Required template an der Service Connection) setzt `Initialize-SeedProject.ps1` ohne weiteren Schalter, sobald die YAML-Datei auf dem Default-Branch `web-app.yml` ab v0.5.0 einbindet. Neue Projekte aus `seed-scaffold` bekommen die Policy ohnehin (`seed-terraform//ado-project`, `pr_validation`, Standard `true`).

- **Warum erst nach dem Umschalten:** Vorher sperrte die Pflicht-Policy direkte Pushes auf den Default-Branch, und die Branch-Prüfung hielte die Testläufe der Migration auf ihrem Branch auf; mit der alten YAML-Datei würden PR-Läufe sogar deployen. Bis dahin übergeht das Skript den Branch-Schutz mit einem Hinweis, ohne Fehler; ein erneuter Lauf nach dem Merge holt ihn nach.
- **`-EnableBranchPolicy`** verlangt den Branch-Schutz: Ist der Default-Branch noch nicht umgestellt, fehlt die Policy, und das Skript endet mit Exit-Code 1 (außer im DryRun). So arbeitet der Modus `enableBranchPolicy` des zentralen Scaffolds.
- **`-SkipBranchPolicy`** lässt ihn ausdrücklich weg, auch nach dem Umschalten, etwa solange ein Projekt noch direkt auf den Default-Branch pushen muss. Vorhandene Policies und Prüfungen bleiben, wie sie sind; entfernen geht nur von Hand. Beide Schalter zusammen sind ein Fehler.
- **Erweiterte Branch-Prüfung bleibt:** Weicht eine vorhandene Branch-Prüfung von `-AllowedBranches` und `-TemplateRefs` ab, etwa weil ein Testprojekt zusätzlich `refs/heads/feat/*` erlaubt, lässt ein erneuter Lauf sie stehen und meldet das. Geändert wird sie nur, wenn der Aufruf `-AllowedBranches`, `-TemplateRefs` oder `-EnableBranchPolicy` nennt.

## Intune: Edge-Erweiterungen verteilen

Auf verwalteten Windows-Geräten installiert Edge Erweiterungen außerhalb des Stores über die Richtlinie `ExtensionSettings` („Configure extension management settings“), ein JSON mit `installation_mode` und `update_url` je Erweiterung. Seed setzt `normal_installed`: Edge installiert die Erweiterung von selbst, die Benutzer können sie ausschalten, aber nicht entfernen. Intune erlaubt diese Einstellung nur in **einer** Richtlinie: Setzen zwei Richtlinien sie, meldet Intune einen Konflikt, und auf den betroffenen Geräten wirkt keine. Deshalb gibt es je Tenant genau eine Pipeline, die genau eine Richtlinie besitzt: `seed-intune` im Repo `seed-tenant` des Azure-DevOps-Projekts, mit [`templates/intune-extensions.yml`](templates/intune-extensions.yml). Welche Erweiterungen dazugehören, steht in der Registerdatei `extensions.yaml` dieses Repos; Änderungen kommen per Pull Request, angewendet wird erst nach Freigabe. Signieren und Veröffentlichen der Erweiterung bleibt Sache des Projekts (Deploy mit `extension.signing`, siehe [Signieren und Veröffentlichen für verwaltete Geräte](#signieren-und-veröffentlichen-für-verwaltete-geräte)); die Registerdatei nennt nur, wo sie liegt.

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
      ref: refs/tags/v0.19.0
      endpoint: github-blackforestsentinel

extends:
  template: templates/intune-extensions.yml@seed
  parameters:
    serviceConnection: sc-seed-intune
    environment: seed-intune
    registry: extensions.yaml
    policyName: 'Sentinel Seed: Edge-Erweiterungen'
    adoptPolicies: ['Kundenportal: Edge-Erweiterung']
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
    owner: it@example.org                    # optional
    comment: Seitenleiste für den Support # optional
```

- **Format:** ein bewusst kleiner Ausschnitt aus YAML, den `intune.py` selbst liest: nur der Schlüssel `extensions`, eine Liste von Einträgen mit genau diesen fünf Schlüsseln, Kommentare, Werte ohne, in `'…'` oder in `"…"` Anführungszeichen; `extensions: []` für keinen Eintrag. Abgelehnt mit Zeilennummer werden Tabulatoren, mehrzeilige Werte, `{…}`, Anker, unbekannte oder doppelte Schlüssel und Werte, die YAML nicht als Text läse (`yes`, `2026-01-01`, Zahlen). Jede gültige Datei liest ein YAML-Parser genauso (die Tests prüfen das gegen PyYAML). PyYAML selbst kommt nicht in die Pipeline, weil es auf dem Agent nicht sicher vorhanden ist und ein `pip install` eine weitere Abhängigkeit aus dem Netz wäre. JSON ginge ohne eigenen Parser, kennt aber keine Kommentare; die Datei pflegen Menschen per Pull Request, und Seed nutzt YAML auch für `project.yaml`.
- **Werte:** `name` 3 bis 40 Zeichen (`a-z`, `0-9`, `-`), `id` 32 Zeichen `a` bis `p`, beide eindeutig. `updateUrl` per `https`, ohne Anmeldedaten, Query, Fragment und `;`, endet auf `/api/extension/updates.xml`.
- **Veröffentlicht?** „Registerdatei prüfen“ lädt jede `updateUrl` (bis zu drei Versuche) und verlangt ein Update-Manifest (`gupdate`, Namespace `http://www.google.com/update2/response`) mit `app/@appid` gleich `id` und einem `updatecheck` mit `version` und `https`-`codebase`. Sonst endet der Lauf mit „Erst veröffentlichen (Deploy des Projekts), dann eintragen“, im Pull Request also vor dem Merge. Weil die Adresse aus dem Pull Request kommt, ruft die Prüfung nur Hosts ab, deren Adressen alle öffentlich sind: keine privaten, Loopback-, Link-Local- und Metadaten-Adressen (IPv4 und IPv6, auch eingebettet wie `::ffff:10.0.0.1`). Das gilt für jede Weiterleitung, und die Verbindung geht an die geprüfte Adresse. Auf einem eigenen Agent im internen Netz wird die Prüfung so keine Sonde für interne Dienste.

### Plan und Apply

- **Soll:** je Eintrag der Registerdatei `"<id>": {"installation_mode": "normal_installed", "update_url": "<updateUrl>"}` im JSON von `ExtensionSettings`; der Plan nennt Erweiterungen kurz als `<id>;<updateUrl>`. Die Benutzer können jede Erweiterung unter `edge://extensions` ausschalten, und Edge schaltet sie danach nicht von selbst wieder ein; erzwingen (`force_installed`) lässt sich keine. Die Pipeline besitzt die ganze Richtlinie: Einträge, die nicht in der Datei stehen (auch `"*"` oder gesperrte IDs), und andere Einstellungen in der Richtlinie entfernt sie; zugewiesen ist sie genau „alle Benutzer“. Verglichen wird der Inhalt des JSON, nicht die Schreibweise. Für jede entfernte Erweiterung warnt der Plan, denn Edge deinstalliert sie auf allen Geräten.
- **Umstellung von v0.15.0:** Bis dahin setzte die Richtlinie `ExtensionInstallForcelist`: erzwungen, nicht ausschaltbar. Der erste Lauf mit der neuen Version ersetzt die Einstellung in derselben Richtlinie (ein `PUT`); der Plan zeigt „ExtensionInstallForcelist durch ExtensionSettings ersetzen“ und keine `-`-Zeilen, solange die Registerdatei dieselben Erweiterungen nennt. Die Richtlinie wird dabei nicht gelöscht, damit Edge keinen Stand ohne die Erweiterungen sieht. Wie Edge den Wechsel von „erzwungen“ zu `normal_installed` behandelt, dokumentiert Microsoft nicht; vor der Freigabe deshalb auf einem Testgerät prüfen: Nach dem Abgleich zeigt `edge://policy` `ExtensionSettings` statt `ExtensionInstallForcelist`, und `edge://extensions` zeigt die Erweiterungen mit Schalter und ihren Daten.
- **Leere Registerdatei:** Der Plan bricht ab, statt die Richtlinie zu leeren, denn das würde alle Erweiterungen deinstallieren. Ist genau das gewollt, die Pipeline von Hand mit „Alle Erweiterungen entfernen“ (`allowEmpty`, `--allow-empty`) starten; dann löscht sie die Richtlinie.
- **Konflikte:** Setzt eine andere Richtlinie des Einstellungskatalogs ebenfalls `ExtensionSettings` für Edge (Gerät oder Benutzer; Chrome zählt nicht) und ist sie zugewiesen, endet der Plan mit einem Fehler, der sie nennt. Entschieden als Fehler, nicht als Warnung: Solange der Konflikt besteht, wirkt die Richtlinie der Pipeline auf den betroffenen Geräten nicht, und Anwenden änderte daran nichts; ein grüner Lauf würde etwas anderes behaupten. Ob sich die Zuweisungen tatsächlich überschneiden (Gruppen), prüft die Pipeline nicht, im Zweifel hält sie an. Eine Richtlinie ohne Zuweisung (oder nur mit Ausschlüssen) wirkt nirgends und ist nur eine Warnung. Abhilfe: deren Einträge in die Registerdatei übernehmen und die Richtlinie löschen. `ExtensionInstallForcelist` in anderen Richtlinien ist eine eigene Einstellung und kein Konflikt, etwa für Erweiterungen aus dem Store. Erzwingt eine zugewiesene davon aber eine Erweiterung aus der Registerdatei, warnt der Plan: Ob sie sich dann noch ausschalten lässt, dokumentiert Microsoft nicht. Den Eintrag dort entfernen.
- **Freigegebener Plan:** `plan` gibt dem Plan eine Kennung (`planId`, Hash der Schritte). `apply` rechnet den Plan neu und bricht ohne Änderung ab, wenn die Kennung nicht mehr stimmt, etwa weil jemand die Richtlinie in Intune geändert hat oder ein zweiter Lauf schneller war. Dann einen neuen Lauf starten.
- **Nach dem Apply** liest das Skript die Richtlinie erneut (bis zu drei Versuche) und endet mit Fehler, wenn noch etwas fehlt.
- **Technik:** Graph beta, Einstellungskatalog (`windows10`, `mdm`), Setting-ID aus einer Liste bekannter Kandidaten (`SETTING_CANDIDATES`), das abhängige Textfeld (der JSON-Text) aus der Definition; Wiederholungen bei 429 und 5xx. Den Graph-Token holt `az account get-access-token --resource-type ms-graph` im Kontext der Service Connection.

### Übernahme einer bestehenden Richtlinie

Gibt es die Richtlinie `policyName` noch nicht, aber genau eine aus `adoptPolicies`, benennt `apply` diese um und gleicht sie an (Plan: „übernehmen und umbenennen“). Sie wird nie gelöscht und neu angelegt: Dazwischen würde Edge die Erweiterungen deinstallieren und danach neu installieren; lokale Daten der Erweiterungen gingen dabei verloren. Passen mehrere, bricht der Plan mit ihrer Liste ab; übernommen wird nur genau eine, sonst ginge eine Liste verloren. Setzt die übernommene Richtlinie noch `ExtensionInstallForcelist`, stellt `apply` sie dabei auf `ExtensionSettings` um (siehe Umstellung oben). Gibt es `policyName` schon, bleibt eine alte Richtlinie mit `ExtensionSettings` ein Konflikt; eine mit `ExtensionInstallForcelist` erzwingt ihre Erweiterungen weiter, der Plan warnt (siehe oben).

Beispiel: Ein Projekt verteilt seine Erweiterung bisher mit einer eigenen Pipeline und der Richtlinie „Kundenportal: Edge-Erweiterung“.

1. Onboarding mit `-AdoptPolicies 'Kundenportal: Edge-Erweiterung'` (landet in `adoptPolicies`).
2. Im Projekt den bisherigen Intune-Schritt entfernen oder dessen Pipeline anhalten, **bevor** `seed-intune` zum ersten Mal anwendet. Sonst findet er seine Richtlinie nach der Umbenennung nicht mehr, legt sie neu an, und beide stehen im Konflikt.
3. Pull Request in `seed-tenant` mit dem Eintrag des Projekts (`id` und `updateUrl` wie bisher). Nach dem Merge zeigt der Plan „übernehmen und umbenennen“, mit derselben Liste ohne Entfernungen.
4. Nach dem Apply der bisherigen Identität das Recht `DeviceManagementConfiguration.ReadWrite.All` entziehen; das Onboarding nennt alle weiteren Identitäten mit diesem Recht.

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
  -Approvers it@example.org -AdoptPolicies 'Alte Edge-Richtlinie' -DryRun
```

1. Identität `sentinel-seed-intune` mit Service Principal
2. Graph `DeviceManagementConfiguration.ReadWrite.All` mit Administratorzustimmung; nennt weitere Identitäten mit diesem Recht
3. Resource Group `rg-seed-intune` (leer) und Reader darauf
4. Service Connection `sc-seed-intune` und Federated Credential
5. Checks „Branch control“ und „Required template“ (vorhandene mit anderen Werten werden aktualisiert)
6. Environment `seed-intune` mit Freigabe durch `-Approvers` (Pflicht); wer den Lauf gestartet hat, gibt nicht selbst frei, außer mit `-RequesterCanApprove`. Ein erneuter Lauf gleicht eine vorhandene Freigabe an (bis v0.18 durfte die startende Person freigeben).
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

Die Prüfung „Required template“ an den Deployment-Service-Connections kennt nur die Tags, die sie nennt. Vor dem Merge jedes Pull Requests, der ein Projekt oder `seed-scaffold` auf den neuen Tag hebt, den Tag dort ergänzen: `Initialize-SeedTenant.ps1` mit der neuen `-SeedPipelinesVersion` bzw. `Initialize-SeedProject.ps1` mit `-RequiredTemplateRefs <alt>,<neu>` (siehe [Versionswechsel mit Required template](#tenant-onboarding)). Sonst scheitert der erste Lauf auf `main` an der Prüfung. Alte Tags danach mit `-RequiredTemplateRefs <neu>` entfernen.

## Sicherheit

Schwachstellen bitte vertraulich melden, nicht als Issue: siehe [SECURITY.md](SECURITY.md).

## Lizenz

MIT, siehe [LICENSE](LICENSE).
