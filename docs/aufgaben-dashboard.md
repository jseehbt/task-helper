
## Anforderung
Windows-Anwendung, die den Überblick über offene Aufgaben behält:
- Mails aus Outlook
- Slack-Nachrichten mit Mention oder in Favoritenkanälen
- Jira-Tickets in bestimmten Status, bei denen ich Assignee bin
- GitHub Issues, bei denen ich Assignee bin
- GitHub PRs, bei denen ich Assignee bin oder um Review gebeten wurde

Randbedingung: Teilweise darf keine KI angebunden werden, es muss eine klassische Anwendung sein.

## Gibt es das fertig?
Nein, nicht sauber. Es gibt Aggregatoren (Akiflow, Sunsama, Morgen, Zapier/Make, self-hosted n8n), aber:
- Filter wie "Slack-Favoritenkanäle" oder "Jira-Status X und ich bin Assignee" werden meist nicht gut abgedeckt
- Einige nutzen KI-Funktionen, was bei den Vorgaben ein Problem sein kann

Selbst bauen ist realistisch, alle Plattformen haben passende APIs.

## APIs

### Outlook
- Microsoft Graph API (`/me/messages` mit `$filter`, Delta-Abfragen für Änderungen)
- App-Registrierung in Entra ID nötig, im Firmen-Tenant oft Admin-Consent
- Alternative ohne Registrierung: Outlook-COM-Automatisierung auf dem Windows-Rechner

### Slack
- Web API
- Mentions: `search.messages` (User-Token mit `search:read`)
- Kanalnachrichten: `conversations.history`
- Favoriten (Sidebar-Bereiche) sind **nicht** per API auslesbar, Kanäle müssen in eigener Konfiguration gepflegt werden
- Slack-App muss ggf. vom Workspace-Admin freigegeben werden

### Jira
- REST API mit JQL, z. B. `assignee = currentUser() AND status in ("In Review", "Blocked")`
- Auth: API-Token (Cloud) oder Personal Access Token (Data Center)

### GitHub
- REST- oder GraphQL-API über die Search-Syntax
  - Issues: `is:issue is:open assignee:@me`
  - PRs: `is:pr is:open assignee:@me` und `is:pr is:open review-requested:@me`
- Auth: Fine-grained PAT oder GitHub CLI

## Architektur
- Hintergrunddienst fragt alle paar Minuten per Polling ab (Webhooks nicht nötig)
- Einheitliches Dashboard als Anzeige
- Mögliche Stacks: .NET-Tray-App (WPF/WinUI), Tauri/Electron, Python-Backend mit lokaler Weboberfläche
- Tokens im Windows Credential Manager statt in Klartext-Configs

## Widget-Optionen

| Option | Eigenschaften |
|---|---|
| Windows-11-Widgets-Board | Windows App SDK (Widget Provider), UI als Adaptive Card, begrenzte Interaktion, MSIX-Paketierung nötig, Board nicht dauerhaft sichtbar |
| Windows 10 | Kein Widget-System für Drittanbieter |
| Rainmeter | Dauerhaft sichtbare Desktop-Widgets, liest JSON vom lokalen Dienst (WebParser), keine Store-Paketierung |
| Eigenes kleines Fenster | Rahmenlos, Always-on-top oder Tray-Flyout aus WPF/WinUI-App, volle Kontrolle über Layout und Klicks, meist am einfachsten |

**Empfehlung:** Dienst und Dashboard bauen, Widget als zweite Ansicht auf denselben Daten. Für dauerhafte Sichtbarkeit: Always-on-top-Fenster oder Rainmeter. Widgets-Board nur, wenn Windows-Look-and-Feel wichtig ist.

Hinweis: Bei eingeschränkten Software-Installationen brauchen Rainmeter und MSIX-Pakete oft eine Freigabe, eine normale Tray-App meist weniger.

## Offen
- Stack-Wahl (.NET oder Python)
- Ob ein Prototyp gebaut werden soll
