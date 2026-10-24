# Sicherheit

## Schwachstelle melden

Sicherheitslücken bitte nicht als öffentliches Issue, Pull Request oder Diskussion melden, sondern vertraulich über GitHub: im Repository unter **Security** → **Report a vulnerability** (Private Vulnerability Reporting). Die Meldung sehen nur die Maintainer des Repositorys.

Hilfreich sind die betroffene Version (Tag), Template oder Skript und Datei, Schritte zum Nachvollziehen und die mögliche Auswirkung, etwa welche Identität oder welches Secret erreichbar wird. Keine echten Zugangsdaten, Tokens, Tenant- oder Subscription-IDs mitschicken.

## Unterstützte Versionen

Korrekturen erscheinen als neues Release der jeweils neuesten Minor-Version. Ältere Versionen bekommen keine Sicherheitskorrekturen; Projekte heben dafür `ref` auf den neuen Tag an. Nach der Korrektur erscheint ein Security Advisory im Repository.
