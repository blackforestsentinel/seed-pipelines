# Sentinel Seed – Pipeline-Templates

Azure-DevOps-Pipeline-Templates für Projekte aus dem [Sentinel-Seed-Template](https://github.com/blackforestsentinel/seed-template). Projekte binden sie per `extends` ein und pinnen einen Tag:

```yaml
resources:
  repositories:
    - repository: seed
      type: github
      name: blackforestsentinel/seed-pipelines
      ref: refs/tags/v0.1.0
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
```

## templates/web-app.yml

| Stage | Inhalt |
| --- | --- |
| `build` | `dotnet test` und `dotnet publish` der API, `npm test` und `npm run build` des Frontends, `terraform fmt` und `validate`, Abgleich mit `project.yaml` |
| `plan_<env>` | `terraform plan` gegen den Remote-State `<project>/<env>.tfstate`, Plan als Artefakt |
| `deploy_<env>` | Deployment-Job auf das Environment `<project>-<env>` (dort hängt die Freigabe), `terraform apply` des Plans, Deploy der Function, `config.json` schreiben, Deploy der Static Web App, Smoke-Test |

Umgebungen laufen in der Reihenfolge der Liste nacheinander.

### Voraussetzungen

- GitHub-Service-Connection in der Azure-DevOps-Organisation (Name frei wählbar, im Projekt als `endpoint` angegeben).
- Azure-Service-Connection mit Workload Identity Federation. Terraform bekommt deren OIDC-Token, es gibt keine Secrets.
- Rollen der Service-Connection-Identität: `Contributor` und `Role Based Access Control Administrator` auf der Subscription, `Storage Blob Data Contributor` auf dem State-Storage.
- Environment `<project>-<env>` je Umgebung, Freigaben als Check am Environment.

### Projektstruktur, die das Template erwartet

`project.yaml`, `api/Api.slnx`, `api/Api/Api.csproj`, `frontend/` mit `npm test` und `npm run build` nach `dist/`, `infra/` mit den Outputs `resource_group_name`, `function_app_name`, `function_app_url`, `static_web_app_name` und `static_web_app_url`. Pfade sind per Parameter änderbar.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
