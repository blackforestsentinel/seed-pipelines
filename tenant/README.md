# seed-tenant: Edge-Erweiterungen per Intune

Dieses Repo enthält die Registerdatei [`extensions.yaml`](extensions.yaml): alle Edge-Erweiterungen der Seed-Projekte, die Intune auf den verwalteten Windows-Geräten installiert. Die Pipeline `__PIPELINE__` (Template `templates/intune-extensions.yml` aus [seed-pipelines](https://github.com/blackforestsentinel/seed-pipelines), README-Abschnitt „Intune: Edge-Erweiterungen verteilen“) hält die Intune-Richtlinie „__POLICY_TEXT__“ gleich dieser Liste, zugewiesen an alle Benutzer.

Intune erlaubt die Einstellung `ExtensionInstallForcelist` nur in **einer** Richtlinie. Andere Richtlinien mit derselben Einstellung führen zu einem Konflikt, und keine wirkt; der Plan bricht dann ab. Erweiterungen gehören deshalb hierher und nicht in eigene Richtlinien der Projekte.

## Erweiterung aufnehmen

1. Das Projekt signiert und veröffentlicht seine Erweiterung selbst (Deploy). Danach liefert `https://<api>/api/extension/updates.xml` die ID der Erweiterung aus.
2. Pull Request auf `main` mit dem neuen Eintrag in `extensions.yaml` (Format im Kopf der Datei).
3. Die Branch-Policy startet die Pipeline als Prüfung: Format der Datei, `updateUrl` per https und auf `/api/extension/updates.xml`, die Datei erreichbar und mit `app/@appid` gleich `id`. Dazu braucht es mindestens eine Freigabe durch eine andere Person als die, die den PR erstellt hat.
4. Nach dem Merge läuft die Pipeline auf `main`: Stage Plan zeigt im Log die Änderungen an der Richtlinie, Stage Apply wartet auf die Freigabe im Environment `__ENVIRONMENT__` und wendet genau diesen Plan an.
5. Edge installiert die Erweiterung beim nächsten Abgleich mit Intune, in der Regel innerhalb einiger Stunden.

## Erweiterung entfernen

Eintrag löschen, sonst wie oben. Edge deinstalliert die Erweiterung dann auf allen Geräten; der Plan warnt dabei für jede entfernte Erweiterung. Eine leere Liste wendet die Pipeline nur in einem von Hand gestarteten Lauf mit „Alle Erweiterungen entfernen“ an; sie löscht dann die Richtlinie.

## Grenzen

- Eine Richtlinie mit einer Zuweisung (alle Benutzer) für alle Erweiterungen; einzelne Erweiterungen nur für bestimmte Gruppen gibt es nicht.
- Einstellungen der Erweiterungen selbst (`managed_schema.json`, unter Windows `HKLM\Software\Policies\Microsoft\Edge\3rdparty\extensions\<id>\policy`) sind nicht Teil davon; die pflegen die Projekte in eigenen Richtlinien.
- Die Richtlinie in Intune nicht von Hand ändern: Der nächste Lauf setzt sie zurück, und ein Apply, dessen Plan sich seit der Freigabe geändert hat, bricht ab.

## Neue Version von seed-pipelines

Erst `onboarding/Initialize-SeedIntune.ps1` mit der neuen `-SeedPipelinesVersion` ausführen (aktualisiert die Prüfung „Required template“ an der Service Connection), dann `ref` in `azure-pipelines.yml` per Pull Request anheben.
