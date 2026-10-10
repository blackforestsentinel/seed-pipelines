# Sentinel Seed – Pipeline-Templates

Azure-DevOps-Pipeline-Templates für Projekte aus dem [Sentinel-Seed-Template](https://github.com/blackforestsentinel/seed-template). Projekte binden sie per `extends` ein und pinnen einen Tag:

```yaml
resources:
  repositories:
    - repository: seed
      type: github
      name: blackforestsentinel/seed-pipelines
      ref: refs/tags/v0.4.0
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
    # frontend: false                             # Projekt ohne Frontend (nur API)
```

## templates/web-app.yml

| Stage | Inhalt |
| --- | --- |
| `build` | `dotnet test` und `dotnet publish` der API, `npm test` und `npm run build` des Frontends (nur mit Frontend), `terraform fmt` und `validate`, Abgleich mit `project.yaml` (Projektname, `hosting.staticWebApp: none` passend zu `frontend: false`) |
| `plan_<env>` | `terraform plan` gegen den Remote-State `<project>/<env>.tfstate`, Plan als Artefakt; merkt sich, ob sich die Infrastruktur ändert |
| `apply_<env>` | Nur bei Änderungen: Deployment-Job auf das Environment `<project>-<env>` (dort hängt die Freigabe), `terraform apply` des geprüften Plans |
| `deploy_<env>` | Deployment-Job auf das Environment `<project>-<env>-app`: Outputs lesen, `config.json` aus dem Output `frontend_config` schreiben, `__API_ORIGIN__` in der CSP ersetzen, Function und Static Web App deployen, Status eigener Domains melden, Smoke-Test |

Ohne Infrastruktur-Änderungen entfällt `apply_<env>` samt Freigabe. Ein Plan, der nur Ressourcen im State verschiebt (`moved`-Blöcke nach einem Update von seed-terraform), zählt nicht als Änderung: Terraform wiederholt die Verschiebung bei jedem Plan und speichert sie mit dem nächsten echten Apply. `deploy_<env>` lässt sich nach einem Fehler einzeln neu starten, weil sie keinen Plan anwendet. Scheitert `apply_<env>` an einem veralteten Plan, braucht es einen neuen Lauf.

Umgebungen laufen in der Reihenfolge der Liste nacheinander.

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
git clone --branch v0.4.0 https://github.com/blackforestsentinel/seed-pipelines.git
cd seed-pipelines
./onboarding/Initialize-SeedTenant.ps1 -SubscriptionId <id> `
  -AzureDevOpsOrganization https://dev.azure.com/<org> -AzureDevOpsProject <projekt> `
  -Approvers it@kunde.de -DryRun
```

1. Deployment-Identität: App-Registrierung ohne Secret
2. Azure: `Contributor` und `Role Based Access Control Administrator` auf der Subscription. Die Bedingung erlaubt nur die Rollen, die Seed-Module vergeben: Storage-Datenrollen, `Key Vault Secrets User` und `Key Vault Secrets Officer`, `Monitoring Metrics Publisher`. Nach einem Seed-Update mit neuen Rollen das Skript erneut ausführen; es ersetzt dann die ältere Bedingung.
3. Microsoft Graph: `Application.ReadWrite.OwnedBy` mit Admin-Consent (Modul `sso`)
4. Terraform-State: Storage Account ohne Shared Key, `Storage Blob Data Contributor` nur auf dem Container
5. Service Connection per Workload Identity Federation
6. GitHub-Service-Connection (Token aus `$env:SEED_GITHUB_TOKEN`, sonst Hinweis)
7. Repo und Pipeline `seed-scaffold`
8. GitHub-Service-Connection für alle Pipelines

Danach einmalig einen PAT anlegen und als geheime Variable `SeedScaffoldPat` an der Pipeline `seed-scaffold` hinterlegen. Scopes: Code (Read, write & manage), Build (Read & execute), Environment (Read & manage), Graph (Read), Project and Team (Read), Pipeline Resources (Use and manage). Das Job-Token der Pipeline reicht nicht, solange „Protect access to repositories in YAML pipelines“ an ist; dann darf es keine Repos anlegen.

## templates/scaffold.yml: neues Projekt anlegen

Die Pipeline `seed-scaffold` (Vorlage: [`scaffold/azure-pipelines.yml`](scaffold/azure-pipelines.yml)) fragt beim Start Name, `sso`, Frontend, Umgebungen und Freigebende ab und legt über das Terraform-Modul `seed-terraform//ado-project` an:

- Repo aus `seed-template` mit `project.yaml` und `azure-pipelines.yml` (ohne Frontend: `hosting.staticWebApp: none` und `frontend: false`)
- Environments `<name>-<env>` (Freigabe für Infrastruktur) und `<name>-<env>-app`
- Pipeline, berechtigt für die Environments; auf Wunsch startet sie den ersten Lauf

Die Azure-Service-Connection berechtigt `seed-scaffold` bewusst nicht selbst: Im ersten Lauf eines neuen Projekts gibt eine Administratorin oder ein Administrator sie einmal frei („Permit“). Der State liegt unter `scaffold/<name>.tfstate`; ein erneuter Lauf ergänzt nur, was fehlt.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
