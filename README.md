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
      ref: refs/tags/v0.5.0
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
    # frontend: false                             # Projekt ohne Frontend (nur API)
```

## templates/web-app.yml

| Stage | Inhalt |
| --- | --- |
| `build` | Ein Job: Abgleich mit `project.yaml` (Projektname, `hosting.staticWebApp: none` passend zu `frontend: false`), `terraform fmt` und `validate`, `dotnet test` und `dotnet publish` der API, `npm test` und `npm run build` des Frontends (nur mit Frontend); im PR-Lauf zusätzlich der Secret-Scan |
| `plan_<env>` | `terraform plan` gegen den Remote-State `<project>/<env>.tfstate`, Plan als Artefakt; merkt sich, ob sich die Infrastruktur ändert. Ohne Änderungen die Outputs als Artefakt `tfoutputs_<env>` |
| `apply_<env>` | Nur bei Änderungen: Deployment-Job auf das Environment `<project>-<env>` (dort hängt die Freigabe), `terraform apply` des geprüften Plans, danach die Outputs als Artefakt `tfoutputs_<env>` |
| `deploy_<env>` | Deployment-Job auf das Environment `<project>-<env>-app`, ohne Checkout und ohne `terraform init`: Outputs aus `tfoutputs_<env>`, `config.json` aus dem Output `frontend_config`, `__API_ORIGIN__` in der CSP ersetzen, Function und Static Web App deployen, Status eigener Domains melden, Smoke-Test |

Ohne Infrastruktur-Änderungen entfällt `apply_<env>` samt Freigabe. Ein Plan, der nur Ressourcen im State verschiebt (`moved`-Blöcke nach einem Update von seed-terraform), zählt nicht als Änderung: Terraform wiederholt die Verschiebung bei jedem Plan und speichert sie mit dem nächsten echten Apply. `deploy_<env>` lässt sich nach einem Fehler einzeln neu starten, weil sie keinen Plan anwendet. Scheitert `apply_<env>` an einem veralteten Plan, braucht es einen neuen Lauf.

Umgebungen laufen in der Reihenfolge der Liste nacheinander.

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

### Smoke-Test

`templates/steps/smoke-test.yml` prüft nach dem Deploy:

- **API:** `/api/health` meldet die Build-Nummer dieses Laufs (`version` aus `SeedHealthReport`, das .NET SDK hängt `+<commit>` an). Bis zu 3 Minuten lang, denn kurz nach dem Deploy antwortet noch die alte Fassung oder ein 503.
- **CORS** (nur mit Frontend): Preflight vom Ursprung der Static Web App ist erlaubt, von einem fremden Ursprung nicht.
- **Token-Pflicht** (nur mit `sso: true` und `smokeTest.protectedPath`): Die angegebene Function ohne `[AllowAnonymous]` antwortet ohne Token mit 401. Das Template trägt dafür `api/me` ein.
- **Frontend:** Startseite und `config.json` erreichbar, `Content-Security-Policy`-Header vorhanden und ohne Platzhalter.

Weitere Prüfungen je Feature kommen als eigener Schritt in `smoke-test.yml`, geschaltet über `condition: eq(variables['feature.<name>'], 'true')`. Der erste Schritt setzt diese Variablen aus `features` in `project.yaml`, das so die einzige Wahrheit bleibt. Projektspezifische Angaben wie Pfade kommen über den Parameter `smokeTest`.

### Voraussetzungen

- GitHub-Service-Connection in der Azure-DevOps-Organisation (Name frei wählbar, im Projekt als `endpoint` angegeben).
- Azure-Service-Connection mit Workload Identity Federation. Terraform bekommt deren OIDC-Token, es gibt keine Secrets.
- Rollen der Service-Connection-Identität: `Contributor` und `Role Based Access Control Administrator` auf der Subscription, `Storage Blob Data Contributor` auf dem State-Storage.
- Je Umgebung zwei Environments, für die Pipeline berechtigt: `<project>-<env>` mit Freigabe als Check (Infrastruktur) und `<project>-<env>-app` für den App-Deploy, dort optional eine Freigabe, etwa in Produktion. Azure DevOps legt sie nicht selbst an.

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

[`onboarding/Initialize-SeedTenant.ps1`](onboarding/Initialize-SeedTenant.ps1) richtet einmal pro Kunde alles ein, was Seed-Projekte brauchen. Es läuft mit dem `az`-Login einer Person, die Owner der Subscription, Global Administrator (oder Privileged Role Administrator) und Projektadministrator in Azure DevOps ist. Jeder Schritt prüft zuerst und legt nur an, was fehlt; mit `-DryRun` zeigt das Skript nur an, was fehlt.

```powershell
git clone --branch v0.5.0 https://github.com/blackforestsentinel/seed-pipelines.git
cd seed-pipelines
./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -Approvers it@kunde.de -DryRun
```

1. Deployment-Identität: App-Registrierung ohne Secret
2. Azure: `Contributor` und `Role Based Access Control Administrator` auf der Subscription. Die Bedingung erlaubt nur die Rollen, die Seed-Module vergeben: Storage-Datenrollen, `Key Vault Secrets User` und `Key Vault Secrets Officer`, `Monitoring Metrics Publisher`. Nach einem Seed-Update mit neuen Rollen das Skript erneut ausführen; es ersetzt dann die ältere Bedingung. Dazu kommt die eigene Rolle `Sentinel Seed Lock Contributor`, die nur Löschsperren lesen, setzen und entfernen darf (Modul `storage`); Contributor allein darf das nicht.
3. Microsoft Graph: `Application.ReadWrite.OwnedBy` mit Admin-Consent (Modul `sso`)
4. Terraform-State: Storage Account ohne Shared Key, `Storage Blob Data Contributor` nur auf dem Container
5. Service Connection per Workload Identity Federation
6. GitHub-Service-Connection (Token aus `$env:SEED_GITHUB_TOKEN`, sonst Hinweis)
7. Repo und Pipeline `seed-scaffold`
8. GitHub-Service-Connection für alle Pipelines

Danach einmalig einen PAT anlegen und als geheime Variable `SeedScaffoldPat` an der Pipeline `seed-scaffold` hinterlegen. Scopes: Code (Read, write & manage), Build (Read & execute), Environment (Read & manage), Graph (Read), Project and Team (Read), Pipeline Resources (Use and manage). Das Job-Token der Pipeline reicht nicht, solange „Protect access to repositories in YAML pipelines“ an ist; dann darf es keine Repos anlegen. Die Person, deren PAT es ist, braucht außerdem „Edit policies“ an den Repos des Projekts (Branch-Policy der PR-Validierung).

## templates/scaffold.yml: neues Projekt anlegen

Die Pipeline `seed-scaffold` (Vorlage: [`scaffold/azure-pipelines.yml`](scaffold/azure-pipelines.yml)) fragt beim Start Name, `sso`, `mcp`, `storage`, Frontend, Umgebungen und Freigebende ab und legt über das Terraform-Modul `seed-terraform//ado-project` an:

- Repo aus `seed-template` mit `project.yaml` und `azure-pipelines.yml` (ohne Frontend: `hosting.staticWebApp: none` und `frontend: false`)
- Environments `<name>-<env>` (Freigabe für Infrastruktur) und `<name>-<env>-app`
- Pipeline, berechtigt für die Environments; auf Wunsch startet sie den ersten Lauf
- Branch-Policy auf `main`: Pull Requests brauchen einen erfolgreichen PR-Lauf dieser Pipeline (siehe PR-Validierung). Die Policy ist Pflicht und sperrt damit direkte Pushes auf `main`.

Die Azure-Service-Connection berechtigt `seed-scaffold` bewusst nicht selbst: Im ersten Lauf eines neuen Projekts gibt eine Administratorin oder ein Administrator sie einmal frei („Permit“). PR-Läufe brauchen keine Freigabe, weil sie die Service Connection nicht verwenden. Der State liegt unter `scaffold/<name>.tfstate`; ein erneuter Lauf ergänzt nur, was fehlt.

Für die Branch-Policy reicht der Scope Code (Read, write & manage) des PATs; die Person, deren PAT es ist, braucht am Repo die Berechtigung „Edit policies“ (Projektadministratoren haben sie). Die Policy setzt voraus, dass das neue Projekt seed-pipelines ab v0.5.0 einbindet (`pipelinesVersion`); ältere Versionen würden in PR-Läufen deployen, `ado-project` bricht dann ab.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
