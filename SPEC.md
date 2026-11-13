# SPEC: Aufgaben-Dashboard (Arbeitstitel „TaskHelper“)

Stand: 2026-11-13 (nach Review und Issue-Abgleich überarbeitet; Dependencies R7 bestätigt) · Grundlage: [docs/aufgaben-dashboard.md](docs/aufgaben-dashboard.md) und Interview

## 1. Ziel und Abgrenzung

Eine Windows-Tray-Anwendung, die offene Aufgaben aus Outlook, Slack, Jira Cloud und GitHub an einer Stelle zeigt. Sie ersetzt keine der Quellen, sondern liefert den Überblick „was wartet auf mich“.

**Grundsätze**

- **Keine KI.** Klassische, deterministische Filterlogik. (Outlooks „Relevant“-Klassifizierung wird als fertiges Attribut von Microsoft gelesen, die App selbst rechnet nichts.)
- **Strikt lesend gegenüber allen Quellen.** Die App schreibt nie in Outlook, Slack, Jira oder GitHub (kein Als-gelesen-Markieren, keine Reaktionen, keine Transitions). Lokale Aktionen (abhaken, pinnen, gesehen) wirken nur lokal.
- **Ein Benutzer, ein Rechner.** Persönliche Laptops, kein geteilter Zustand, keine Synchronisation zwischen Geräten.
- **v1 nur für mich, firmenweite Verteilung später möglich.** Keine Hardcodes von Tenant, Client-IDs, Workspace, Org oder persönlichen Filtern; alles über Config. Eine Überarbeitung vor der Verteilung ist eingeplant (siehe §12).

**Nicht im Umfang v1**

- Volles Hauptfenster, Einstellungsdialog für Filter, Suche
- Always-on-top-Widget, Rainmeter, Windows-11-Widgets-Board
- Toast-Benachrichtigungen
- Fälligkeiten (Jira `duedate`, Outlook-Flag-Fälligkeit)
- Snooze
- Manueller Pause-Schalter
- Mehrere Instanzen je System (ein Tenant, ein Slack-Workspace, eine Jira-Cloud-Site, eine GitHub-Org)
- Slack-Gruppen-DMs, User-Group-Mentions, GitHub-Team-Review-Requests

## 2. Technischer Rahmen

| Thema | Entscheidung |
|---|---|
| Laufzeit | .NET 10 (LTS), `net10.0-windows`, WPF |
| Auslieferung v1 | `dotnet publish` self-contained, single-file, `win-x64`, mit `IncludeNativeLibrariesForSelfExtract=true` (native Bibliotheken von WPF, SQLite `e_sqlite3` und MSAL-Broker `msalruntime`). Kein Trimming (von WPF nicht unterstützt). |
| Prozessmodell | Ein Prozess: Tray-App mit integriertem Polling, kein Windows-Dienst |
| Autostart | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, im Tray-Menü schaltbar, keine Admin-Rechte |
| Einzelinstanz | Named Mutex im Namensraum `Local\` (pro Anmeldesitzung); zweiter Start bringt das Flyout der laufenden Instanz nach vorn (Named Pipe/Window-Message) |
| Persistenz | SQLite über `Microsoft.Data.Sqlite` in `%LOCALAPPDATA%\TaskHelper\state.db` |
| Konfiguration | JSON in `%APPDATA%\TaskHelper\config.json` |
| Secrets | Windows Credential Manager (P/Invoke `CredRead`/`CredWrite`/`CredDelete`, Typ `CRED_TYPE_GENERIC`); Graph-Tokens über MSAL |
| Entwicklung | Unter Windows (Repo wird nach Erstellung der Spec dort geklont) |

### Projektstruktur

Auch wenn unter Windows entwickelt wird, wird die Logik von der UI getrennt, damit sie ohne UI testbar ist:

- `TaskHelper.Core` (`net10.0`): Item-Modell, Quellen-Adapter, Polling-Scheduler, Merge-/Dedup-Logik, Persistenz, Config-Parsing. Keine WPF-/WinForms-Referenzen.
- `TaskHelper.App` (`net10.0-windows`, WPF): Tray-Icon, Flyout, Token-Dialog, Credential-Manager-Zugriff, Session-Events, Autostart.
- `TaskHelper.Core.Tests`: Unit-Tests mit HTTP-Fixtures.

### Dependencies (vor Einführung bestätigen)

| Paket | Zweck | Status |
|---|---|---|
| `Microsoft.Data.Sqlite` | Persistenz | im Interview bestätigt |
| `Microsoft.Identity.Client` | Graph-Login (MSAL, Public Client) | bestätigt (2026-11-13) |
| `Microsoft.Identity.Client.Broker` | Login über WAM (Windows-Konto, SSO, OS-verwaltete Tokens) | bestätigt (2026-11-13) |
| xUnit | Tests | bestätigt (2026-11-13) |

Tray-Icon über `System.Windows.Forms.NotifyIcon` (`UseWindowsForms=true`), damit kein zusätzliches Paket nötig ist. JSON über `System.Text.Json`, Logging mit einem eigenen kleinen Datei-Logger statt `Microsoft.Extensions.Logging`, solange nicht anders gewünscht.

## 3. Datenmodell

### Item (normalisiert, aus den Quellen)

| Feld | Beschreibung |
|---|---|
| `Key` | Stabil und eindeutig: `{source}:{instance}:{externalId}` (Format siehe unten). Die Instanzkennung ist schon in v1 enthalten, damit spätere Erweiterungen auf mehrere Sites/Orgs die Datenbank nicht brechen (§12). |
| `Source` | `Outlook`, `Slack`, `Jira`, `GitHub` |
| `Reasons` | Menge der Gründe, warum das Item da ist (siehe unten), mehrere gleichzeitig möglich. Jeder Reason hat eine Art und ein optionales Detail (z. B. Name der Jira-Abfrage). |
| `Flags` | Zusatzinformationen für die Anzeige, die keine Gründe sind: `IsDraft` (GitHub), `UnreadCount` (Slack-DM, Sammel-Item) |
| `Title` | Betreff, Ticket-/PR-Titel, Nachrichtenvorschau (Slack: erste ~120 Zeichen, Mentions/Links aufgelöst) |
| `Context` | Absender, Kanal, `PROJ-123 · In Review`, `repo#42` |
| `Timestamp` | Zeitpunkt der letzten relevanten Aktivität (Mail-Eingang, Nachrichtenzeit, `updated`/`updated_at`) |
| `ChangeKey` | Wert, dessen Änderung ein abgehaktes Item wieder sichtbar macht (siehe §6) |
| `Urls` | Browser-URL, optional Desktop-Ziel |
| `Highlight` | z. B. `@here`/`@channel`/`@everyone` in Kanalnachricht |

**Key-Format**

| Quelle | Instanz | Beispiele |
|---|---|---|
| Outlook | Objekt-ID des angemeldeten Benutzers (`/me` → `id`) | `outlook:{userId}:{immutableId}` |
| Slack | Team-ID aus `auth.test` | `slack:T0123:C123/1699999999.000100` (Nachricht), `slack:T0123:dm/D456` (DM-Konversation), `slack:T0123:overflow/C123` (Sammel-Item) |
| Jira | Host der Site-URL | `jira:example.atlassian.net:10042` |
| GitHub | Org | `github:example-org:PR_kwDO…` (`node_id`) |

**Reasons**

- Outlook: `Flagged`, `UnreadFocused`
- Slack: `Mention`, `DirectMessage`, `ChannelUnread`
- Jira: `JqlMatch` (Detail: Name der Abfrage)
- GitHub: `IssueAssigned`, `PrAssigned`, `ReviewRequested`

### Lokaler Zustand (pro `Key`)

| Feld | Bedeutung |
|---|---|
| `FirstSeenAt` | Erstes Auftauchen in einem Poll (bzw. Wiederauftauchen nach `GoneAt`) |
| `LastPresentAt` | Letzter erfolgreicher, vollständiger Poll, in dem das Item enthalten war |
| `LastChangeAt` | Zeitpunkt, zu dem die App das Item erstmals, mit neuem `ChangeKey` oder als abgehaktes Item mit neuem Reason (§6) gesehen hat (für „neu“ am Tray-Icon, §8.1) |
| `GoneAt` | Gesetzt, wenn ein vollständiger Poll der Quelle das Item nicht mehr enthält; sonst leer |
| `Pinned` | Manuell nach oben |
| `SeenAt`, `SeenChangeKey` | Explizit als gesehen markiert oder angeklickt; `ChangeKey` zu diesem Zeitpunkt (für Hervorhebung „neu“ in der Liste) |
| `DismissedAt`, `DismissedChangeKey`, `DismissedReasons` | Lokal abgehakt; `ChangeKey` und Reasons zum Zeitpunkt des Abhakens |
| `LastPayload` | Letzter bekannter Item-Stand für die Anzeige nach Neustart/offline |

Zusätzlich je Quelle: `LastSuccessAt`, `LastError`, `TokenExpiresAt` (soweit bekannt), Slack-Baseline-Zeitpunkt und Zeitpunkt der letzten erfolgreichen Mention-Suche (§4.2).

Zusätzlich für Slack eine eigene Tabelle der bekannten Mentions (Key, `ts`, `thread_ts`, Status offen/erledigt, letzte Prüfung), siehe §4.2. Sie unterliegt nicht der 30-Tage-Regel unten; erledigte Einträge werden nach 90 Tagen gelöscht.

**UI-Zustand** (ebenfalls in der SQLite-Datenbank): Ansicht (gruppiert/Liste), Sortierung, eingeklappte Gruppen, `LastFlyoutOpenedAt`.

**Verschwinden und Wiederauftauchen**

- Items, die eine Quelle in einem **erfolgreichen und vollständigen** Poll (§4) nicht mehr liefert, verschwinden aus der Anzeige; `GoneAt` wird gesetzt. Fehlerhafte oder unvollständige Polls setzen nie `GoneAt`.
- Taucht ein Item mit gesetztem `GoneAt` wieder auf, gilt es als neu: `FirstSeenAt`, `LastChangeAt` = jetzt; `GoneAt`, `Seen*` und `Dismissed*` werden zurückgesetzt. `Pinned` bleibt erhalten.
- Der lokale Zustand eines verschwundenen Items wird gelöscht, wenn `LastSuccessAt` der Quelle mehr als 30 Tage nach `GoneAt` liegt. Maßgeblich ist also die Zeit, in der die Quelle erfolgreich abgefragt wurde, nicht die Uhrzeit: Urlaub, Pausen oder ein abgelaufenes Token löschen keinen Zustand.

**Datenschutz:** Inhalte (Betreffs, Vorschauen) werden im Klartext im Benutzerprofil gespeichert. Das reicht, weil die Laptops persönlich sind und `%LOCALAPPDATA%` nur für den Benutzer lesbar ist. Logs enthalten nie Inhalte oder Tokens, nur Keys, Zähler, HTTP-Status und Fehlertypen.

## 4. Quellen

Alle Adapter implementieren dieselbe Schnittstelle: `Task<SourceResult> PollAsync(CancellationToken)`. Ergebnis ist eine Item-Menge mit Flag `IsComplete` oder ein Fehler.

- **Vollständig** (`IsComplete = true`) ist ein Ergebnis nur, wenn alle Teilabfragen der Quelle (Outlook 2, GitHub 3, Jira n, Slack Mentions/DMs/Kanäle) erfolgreich waren und keine davon gekürzt wurde (Obergrenzen, GitHub `incomplete_results`).
- Teile, die nach einer Drosselungsregel in diesem Zyklus bewusst übersprungen werden (Slack: alte DMs und Mentions nur jeden 6. Zyklus), übernehmen ihren letzten Stand und machen das Ergebnis nicht unvollständig.
- Nur ein vollständiges Ergebnis darf Items als verschwunden markieren (§3). Bei einem unvollständigen Ergebnis werden gelieferte Items aktualisiert, fehlende behalten ihren letzten Stand.
- Liefert eine Quelle einen Fehler, bleibt der letzte Stand sichtbar und wird als veraltet markiert. Ein Fehler darf nie dazu führen, dass Items als „verschwunden“ gelten.

### 4.1 Outlook (Microsoft Graph)

**Auth**

- Entra-App-Registrierung als Public Client, Delegated Permissions `Mail.Read`, `User.Read`.
- MSAL interaktiv mit WAM-Broker; danach `AcquireTokenSilent`. Tenant-ID und Client-ID kommen aus der Config.
- Die App-Registrierung braucht die Redirect-URI `ms-appx-web://microsoft.aad.brokerplugin/{clientId}`. Der interaktive Login braucht ein Parent-Fenster (`WithParentActivityOrWindow`); dafür wird das Handle des Token-Dialogs bzw. Flyouts übergeben, aus dem die Anmeldung gestartet wurde.
- Der Broker erfüllt Conditional-Access- und Device-Compliance-Richtlinien und hält die Tokens im Betriebssystem; das ist der Grund, ihn statt eines eigenen Token-Caches zu nutzen.
- Schlägt der stille Abruf fehl (`MsalUiRequiredException`), geht die Quelle in den Fehlerzustand „Anmeldung erforderlich“. Die interaktive Anmeldung startet nur nach Klick, nie unaufgefordert aus dem Polling heraus.
- Header `Prefer: IdType="ImmutableId"`, damit IDs beim Verschieben stabil bleiben.

**Abfragen**

1. Markiert: `GET /me/messages?$filter=flag/flagStatus eq 'flagged'` (alle Ordner außer „Gelöschte Elemente“ und „Junk-E-Mail“; clientseitig über `parentFolderId` gefiltert, die IDs der beiden Ordner kommen einmalig aus `/me/mailFolders/deleteditems` bzw. `/junkemail`)
2. Ungelesen/Relevant: `GET /me/mailFolders/inbox/messages?$filter=isRead eq false and inferenceClassification eq 'focused'` (ohne den `inferenceClassification`-Teil, wenn `unread.focusedOnly: false`)

Jeweils mit `$select=id,subject,from,toRecipients,receivedDateTime,lastModifiedDateTime,webLink,flag,isRead,internetMessageId,parentFolderId` und Paging über `@odata.nextLink`. Ist die Ergebnismenge sehr groß, wird bei 500 Items abgeschnitten und ein Hinweis angezeigt; das Ergebnis gilt dann als unvollständig (§4).

**Clientseitige Filter (nur für Abfrage 2, „ungelesen“)**

- Eigene Adresse muss in `toRecipients` stehen; CC und Verteiler fallen raus (abschaltbar über `unread.toOnly: false`). Die eigene Adresse und Aliase kommen aus `/me` (`mail`, `proxyAddresses` ist delegiert nicht immer lesbar, daher zusätzlich `outlook.ownAddresses` in der Config).
- Ausschlussliste aus der Config: Absender-Pattern (z. B. `*@github.com`, `jira@*.atlassian.net`, `noreply@*`) und Betreff-Pattern (Wildcard, case-insensitive). Damit doppeln Jira-/GitHub-Benachrichtigungen nicht die eigentlichen Items.

Markierte Mails werden **nicht** gefiltert; das Flag ist eine bewusste Handlung.

**Dedup:** Eine Mail, die markiert und ungelesen ist, ergibt ein Item mit beiden Reasons.

**Erledigt:** Das Item verschwindet, wenn das Flag entfernt oder abgeschlossen wird bzw. die Mail gelesen ist. Lokales Abhaken ist zusätzlich möglich (§6).

**ChangeKey:** `receivedDateTime`, also faktisch konstant. `lastModifiedDateTime` wäre zu laut: Schon das Lesen einer markierten Mail ändert es und würde ein abgehaktes Item zurückholen. Relevante Änderungen (Mail wieder ungelesen markiert) kommen über die Reason-Regel beim Abhaken (§6) zurück.

**Klickziel**

- Browser: `webLink`
- Desktop (optional): Graph-ID über `POST /me/translateExchangeIds` in eine EntryID umwandeln, dann `outlook.exe /select "outlook:{EntryID}"`. Das funktioniert nur mit dem klassischen Outlook; schlägt es fehl, wird `webLink` geöffnet. Default: Browser.

### 4.2 Slack (Web API, User-Token)

**Auth**

- Interne Slack-App im Workspace mit User-Token (`xoxp-`).
- Scopes: `search:read`, `channels:read`, `channels:history`, `groups:read`, `groups:history`, `im:read`, `im:history`, `users:read`.
- Das Token wird über den Token-Dialog im Credential Manager abgelegt.
- Eigene User-ID, Team-ID und Workspace-URL werden per `auth.test` ermittelt.
- Slack meldet Auth-Fehler mit HTTP 200 und `ok: false`. Die Fehlercodes `invalid_auth`, `not_authed`, `token_revoked`, `token_expired`, `account_inactive` und `missing_scope` führen zu `AuthRequired` (§7), andere `ok: false`-Antworten zu `Error`.
- Benutzernamen (für Absender, aufgelöste Mentions im Titel) und die Merkmale `is_bot`/`deleted` kommen aus `users.info` und werden im Speicher gecacht (Lebensdauer: Prozess, höchstens 24 h). Unbekannte IDs werden einzeln nachgeladen.

**Hinweis Rate-Limits:** Die seit 2025 verschärften Limits für `conversations.history`/`conversations.replies` gelten für kommerziell verteilte Apps außerhalb des Marketplace. Intern im Workspace erstellte Apps sind ausgenommen. Die App muss deshalb als interne App des eigenen Workspace angelegt werden. Unabhängig davon werden 429-Antworten mit `Retry-After` respektiert.

**a) Mentions**

- **Entdecken:** `search.messages` mit einer Abfrage auf die eigene User-ID, sortiert nach Zeit, paginiert. Die genaue Query-Syntax (`<@Uxxxx>` vs. `@handle`) wird im ersten Spike gegen den echten Workspace geprüft.
- **Suchfenster:** `after:` = Datum von max(Baseline, letzte erfolgreiche Mention-Suche − 2 Tage). Die Suche liefert also nur neue Mentions und wächst nicht mit der Zeit. (`after:` arbeitet tagesgenau; die 2 Tage Überlappung fangen Verzögerungen im Suchindex ab.)
- **Mention-Tabelle:** Neu gefundene Mentions werden in die Slack-Mention-Tabelle (§3) eingetragen. Die offenen Mentions aus dieser Tabelle sind die maßgebliche Menge für das Ergebnis, nicht das Suchergebnis.
- **Baseline:** Beim ersten Start wird der Zeitpunkt gespeichert. Mentions davor zählen nie. Bei Neuinstallation (leere DB) gilt dasselbe.
- **Erledigt-Prüfung:** Für jede offene Mention `conversations.replies` auf den Thread (`thread_ts`, bzw. `ts`, wenn die Mention selbst Top-Level ist). Gibt es dort eine Nachricht von mir mit `ts` > Mention-`ts`, gilt die Mention als erledigt und wird in der Tabelle als erledigt gespeichert. Sie wird danach nicht mehr geprüft.
  - Mentions älter als 30 Tage werden nur in jedem 6. Zyklus geprüft.
  - Lokal abgehakte Mentions werden nicht mehr geprüft (ihr `ChangeKey` ändert sich nie, sie bleiben ohnehin ausgeblendet).
  - Ist die Nachricht oder der Thread gelöscht oder der Kanal nicht mehr zugänglich (`thread_not_found`, `message_not_found`, `channel_not_found`), gilt die Mention als erledigt.
- Eine Antwort im Kanal außerhalb des Threads zählt nicht.
- Erwähnt mich jemand nach meiner Antwort erneut, entsteht daraus ein **neues Item**. Andere Antworten nach meiner reaktivieren nichts.
- Eigene Nachrichten, die mich selbst erwähnen, werden ignoriert.
- Mention in einer DM wird als DM behandelt (b). Mentions in Gruppen-DMs (`is_mpim`) werden verworfen, weil Gruppen-DMs nicht im Umfang sind.
- Mention in einem konfigurierten Kanal ergibt ein Item mit den Reasons `Mention` + `ChannelUnread`. Es verschwindet erst, wenn **beide** Bedingungen entfallen sind (Mention erledigt **und** Kanal gelesen) oder es lokal abgehakt wird.

**b) Ungelesene DMs (nur 1:1)**

- `users.conversations?types=im` liefert die DM-Liste. Für jede DM liefert `conversations.info` `last_read`, danach holt `conversations.history?oldest={last_read}` die ungelesenen Nachrichten anderer.
- Ein Item pro DM-Konversation (nicht pro Nachricht, Key `slack:{T}:dm/{D}`): Titel = letzte ungelesene Nachricht, Kontext = Absender + „N ungelesen“ (`Flags.UnreadCount`).
- Ignoriert werden DMs mit Slackbot (`USLACKBOT`), Bots/Apps und deaktivierten Benutzern sowie Nachrichten mit `subtype` (Systemnachrichten).
- Verschwindet, sobald `last_read` in Slack nachzieht.
- **Kostenrisiko:** Bei vielen DMs sind das viele Calls. Gegenmaßnahmen: DMs ohne Aktivität seit 30 Tagen nur noch in jedem 6. Zyklus prüfen; `latest` aus `conversations.info` nutzen, falls vorhanden, um `history`-Calls zu sparen. Wird im Spike gemessen.

**c) Konfigurierte Kanäle** (Ersatz für Favoriten und „bei allen neuen Nachrichten benachrichtigen“, da beides nicht über die offizielle API lesbar ist)

- Kanäle werden per ID in der Config gelistet; die Namen kommen über `conversations.info`.
- Ungelesen = Top-Level-Nachrichten mit `ts` > `last_read` (`conversations.history?oldest={last_read}`), maximal `maxUnreadPerChannel` (Default 50) pro Kanal, und zwar die **neuesten**. Bei mehr erscheint für den Rest ein Sammel-Item „#kanal: N weitere ungelesen“ (Key `slack:{T}:overflow/{C}`, ChangeKey = N). Solange ein Kanal über der Grenze liegt, gilt das Slack-Ergebnis als unvollständig (§4); ältere Items dieses Kanals verschwinden dann nicht, nur weil sie aus den neuesten 50 herausgefallen sind.
- Ein Item pro Nachricht. Thread-Antworten erscheinen nicht, außer sie wurden in den Kanal gepostet („also send to channel“, `subtype: thread_broadcast`).
- `@here`/`@channel`/`@everyone` (`<!here>`, `<!channel>`, `<!everyone>`) im Text setzt `Highlight` und wird visuell hervorgehoben. In nicht konfigurierten Kanälen werden sie bewusst nicht erkannt.
- Bot- und eigene Nachrichten werden ignoriert, ebenso Join-/Leave-Nachrichten (`subtype`).
- Verschwindet, wenn `last_read` nachzieht (Kanal in Slack gelesen) oder bei lokalem Abhaken.

**ChangeKey Slack:** Nachrichten-`ts` bzw. für DMs `ts` der neuesten ungelesenen Nachricht. Ein abgehakter DM-Eintrag taucht also bei einer neuen Nachricht wieder auf.

**Klickziel**

- Browser: Permalink, ohne zusätzlichen API-Call selbst gebaut aus der Workspace-URL (`auth.test` → `url`): `{url}archives/{C}/p{ts ohne Punkt}`, für Thread-Antworten mit `?thread_ts={thread_ts}&cid={C}`.
- Desktop: `slack://channel?team={T}&id={C}`. Ob sich darüber auch eine einzelne Nachricht anspringen lässt (`&message={ts}`), ist nicht offiziell dokumentiert und wird im Spike geprüft. Funktioniert es nicht, wird der https-Permalink geöffnet; die Slack-Desktop-App übernimmt ihn, wenn sie so eingestellt ist.
- Default: Desktop.

### 4.3 Jira Cloud

**Auth:** Basic mit E-Mail + API-Token gegen die Site-URL. Site-URL und E-Mail stehen in der Config, das Token im Credential Manager. In v1 wird nur das klassische API-Token ohne Scopes unterstützt; scoped API-Tokens funktionieren nur über `https://api.atlassian.com/ex/jira/{cloudId}/…` und sind nicht im Umfang (Risiko §13). Der Token-Dialog weist darauf hin.

**Abfrage:**

- Ein oder mehrere benannte, frei konfigurierte JQL-Strings, z. B. `assignee = currentUser() AND status in ("In Review", "Blocked")`.
- Endpoint `GET /rest/api/3/search/jql` (der alte `/search` ist entfernt), Paging über `nextPageToken`, `fields=summary,status,updated,project,issuetype`.
- Treffer mehrerer Abfragen werden zu einem Item mit mehreren Reasons zusammengeführt (Detail je Reason: Name der Abfrage).

**Fehler:** Ungültiges JQL (HTTP 400) gibt den Fehlerzustand mit dem Namen der Abfrage und der Jira-Fehlermeldung, damit sich der Fehler in der Config finden lässt. Die übrigen Abfragen laufen weiter: Ihre Treffer werden übernommen, das Ergebnis gilt aber als unvollständig (§4), damit Items der fehlerhaften Abfrage nicht als verschwunden gelten.

**ChangeKey:** `fields.updated`

**Klickziel:** `{site}/browse/{key}` (nur Browser)

### 4.4 GitHub (github.com, eine Org)

**Auth:** Fine-grained PAT für die konfigurierte Org, Repository-Zugriff „All repositories“ mit Read auf Issues, Pull requests und Metadata. Hat die Org SAML-SSO, muss der Token autorisiert sein; ein 403 mit SSO-Hinweis wird als eigener Fehlertext angezeigt. Verlangt die Org eine Freigabe für fine-grained PATs, ist der Token bis zur Freigabe wirkungslos (Risiko §13).

**Abfragen** (REST `GET /search/issues`, jeweils mit `org:{org} archived:false`):

1. `is:issue is:open assignee:@me`
2. `is:pr is:open assignee:@me`
3. `is:pr is:open user-review-requested:@me` (nur direkte Anfragen, keine Teams)

**Dedup:** Ein PR, der zugewiesen ist und für den ein Review angefragt ist, ergibt ein Item mit beiden Reasons. Drafts werden angezeigt und markiert (`Flags.IsDraft`).

**Paging:** Jede Suche mit `per_page=100`, weitere Seiten über den `Link`-Header (`rel="next"`). Ohne `per_page` liefert GitHub nur 30 Treffer; fehlende Treffer würden sonst als verschwunden gelten.

**Vollständigkeit:** Liefert eine Suche `incomplete_results: true` (Timeout im Suchindex), gilt das Ergebnis als unvollständig (§4). Dasselbe gilt, wenn mehr als 1000 Treffer gemeldet werden (Obergrenze der Search-API).

**Erledigt:** Das Item verschwindet, wenn das Issue oder der PR geschlossen ist, die Zuweisung entfernt wurde oder das Review abgegeben ist (GitHub entfernt den Review-Request dann).

**Rate-Limit:** Search erlaubt 30 Requests/min; 3 Requests pro Zyklus sind unkritisch.

**Token-Ablauf:** Der Response-Header `github-authentication-token-expiration` wird gespeichert und für die Vorwarnung genutzt (§7).

**ChangeKey:** `updated_at`

**Klickziel:** `html_url` (nur Browser)

## 5. Polling

- Ein Scheduler, Quellen werden parallel und unabhängig voneinander abgefragt.
- Default-Intervall 5 min, pro Quelle in der Config überschreibbar (Minimum 1 min). ±10 % Jitter.
- „Jetzt aktualisieren“ im Flyout und Tray-Menü stößt alle Quellen sofort an. Es übergeht ein laufendes Backoff, aber nicht ein von der Quelle gesetztes `Retry-After`.
- Ein laufender Poll einer Quelle wird nicht doppelt gestartet.
- Timeout pro Request: 30 s. Bei 429/5xx wird `Retry-After` beachtet, sonst gilt ein exponentielles Backoff bis maximal 30 min.
- Bei `AuthRequired` (§7) pollt die Quelle nicht weiter, auch nicht mit Backoff. Sie startet erst wieder, wenn im Token-Dialog ein Token gespeichert bzw. die Anmeldung abgeschlossen wurde, oder nach „Jetzt aktualisieren“.
- **Pause**
  - Bildschirm gesperrt (`SystemEvents.SessionSwitch`, abschaltbar über `polling.pauseWhenLocked`): Polling ruht. Beim Entsperren läuft sofort ein Poll.
  - Außerhalb der Arbeitszeit (Config: Wochentage + Zeitfenster, z. B. Mo–Fr 07:30–18:30): Polling ruht. Zu Beginn des Fensters läuft sofort ein Poll.
  - Während einer Pause zeigt das Flyout den letzten Stand plus „pausiert seit …“.
  - Ein manueller „Jetzt aktualisieren“ ist auch während einer Pause erlaubt.
- Nach Ruhezustand/Standby (`PowerModeChanged`, Resume) läuft ein sofortiger Poll, sofern keine Pause greift.
- Config-Änderungen (FileSystemWatcher) lösen ein Neuladen aus. Editoren speichern oft über Temp-Datei und Umbenennen und erzeugen mehrere Events; deshalb wird mit ca. 500 ms entprellt, und ist die Datei noch gesperrt, wird das Lesen kurz wiederholt. Ungültige Config: Die alte bleibt aktiv, der Fehler wird im Flyout angezeigt.

## 6. Lokale Aktionen und Lebenszyklus

| Aktion | Wirkung |
|---|---|
| **Abhaken** | Item wird ausgeblendet, `DismissedChangeKey` = aktueller `ChangeKey`, `DismissedReasons` = aktuelle Reasons. Es wird wieder sichtbar, sobald die Quelle einen anderen `ChangeKey` liefert **oder** ein Reason hinzukommt, der beim Abhaken nicht vorlag. Fällt nur ein Reason weg (z. B. Mail gelesen, Flag bleibt), bleibt es ausgeblendet. Kommt es zurück, ist es wieder „neu“ (`LastChangeAt` = jetzt, `Seen*` zurückgesetzt). Verlässt das Item die Quelle und kommt später wieder, gilt es als neu (§3). |
| **Gesehen** | Entfernt die „neu“-Hervorhebung am Item (`SeenChangeKey` = aktueller `ChangeKey`); das Item bleibt in der Liste. Ein Klick auf das Item (öffnen) setzt ebenfalls „gesehen“. Ändert sich der `ChangeKey` später, ist das Item wieder „neu“. |
| **Pinnen** | Item steht immer oben (in Gruppenansicht oben in seiner Gruppe). Toggle. |
| **Rückgängig** | Nach dem Abhaken zeigt das Flyout 5 s lang „Rückgängig“. |

**„Neu“ in der Liste:** Ein Item ist neu, solange `SeenChangeKey` leer ist oder vom aktuellen `ChangeKey` abweicht. Ein abgehaktes Item, das durch einen neuen `ChangeKey` zurückkommt, ist damit automatisch wieder hervorgehoben.

**Tradeoff Abhaken:** Jede Änderung in der Quelle (bei Jira/GitHub auch Labels, Bot-Kommentare, CI-Status-Updates via `updated_at`) holt ein abgehaktes Item zurück und markiert es als neu. Das ist bewusst gewählt: lieber einmal zu viel wieder auftauchen als eine echte Änderung verpassen. Wird das in der Praxis zu laut, kann der ChangeKey später pro Quelle verfeinert werden (z. B. GitHub: nur neue Kommentare/Commits).

Abgehakte Items sind über einen Filter „Abgehakte anzeigen“ im Flyout erreichbar und können dort wiederhergestellt werden.

## 7. Fehler- und Token-Zustände

Jede Quelle hat einen Status: `Ok`, `Stale` (letzter Erfolg älter als 3 × Intervall), `Error` (letzter Poll fehlgeschlagen), `AuthRequired` (401/403/MSAL-UI-Required), `NotConfigured`, `Paused`.

Treffen mehrere zu, gilt die Priorität `NotConfigured` > `AuthRequired` > `Error` > `Paused` > `Stale` > `Ok`. Während einer Pause wird eine Quelle also nie `Stale`.

- `NotConfigured` gilt auch, wenn eine Quelle in der Config aktiviert ist, aber noch kein Token im Credential Manager liegt bzw. bei Outlook noch nie eine Anmeldung stattgefunden hat. Die Statuszeile zeigt dann „Token fehlt“ bzw. „Nicht angemeldet“ mit Link zum Token-Dialog; es gibt kein Fehler-Overlay im Tray.
- `AuthRequired` setzt voraus, dass ein Token bzw. eine Anmeldung vorhanden war und von der Quelle abgelehnt wurde: HTTP 401/403 (GitHub, Jira, Graph), `MsalUiRequiredException` (Graph) oder die Slack-Fehlercodes aus §4.2.
- Eine ungültige Config (§5) wird im Flyout als eigene Hinweiszeile mit Fehlertext und JSON-Pfad angezeigt.

- **Im Flyout:** Pro Quelle Statuszeile mit „aktualisiert vor X min“. Bei Fehlern Warnsymbol und kurzer Fehlertext. Items einer Quelle im Status `Error`, `AuthRequired` oder `Stale` bleiben sichtbar, aber ausgegraut mit Hinweis „Stand von …“. Bei `Paused` werden Items normal angezeigt; der Hinweis „pausiert seit …“ steht in der Fußzeile.
- **Tray-Icon:** Fehler-Overlay (siehe §8), sobald eine konfigurierte Quelle `Error`/`AuthRequired` hat.
- **Vorwarnung vor Ablauf**
  - GitHub: Ablaufdatum aus dem Response-Header.
  - Jira: Ablaufdatum wird im Token-Dialog optional eingetragen (Atlassian liefert es nicht über die API).
  - Slack: User-Tokens laufen ohne Token-Rotation nicht ab, es gibt keine Vorwarnung.
  - Graph: MSAL erneuert selbst, es gibt keine Vorwarnung.
  - Ab 14 Tagen vor Ablauf zeigt die Quelle einen Hinweis im Flyout; ab 3 Tagen zusätzlich das Fehler-Overlay im Tray (gelb statt rot).
- Bei `AuthRequired` gibt es in der Statuszeile einen Link „Token aktualisieren“ bzw. „Anmelden“ (Graph). Der Dialog öffnet sich nicht unaufgefordert.

## 8. UI

### 8.1 Tray-Icon

- Basis-Icon mit gerendertem Zähler-Badge = Anzahl sichtbarer offener Items (ohne Abgehakte, mit ausgegrauten Items fehlerhafter Quellen, unabhängig von Filter-Chips im Flyout), „99+“ ab 100. Das Icon wird zur Laufzeit gezeichnet und muss bei 100/125/150/200 % DPI lesbar sein.
- **Farbe/Akzent**, wenn es neue Items gibt. Neu heißt: `LastChangeAt` eines sichtbaren Items liegt nach `LastFlyoutOpenedAt`. Damit zählen auch abgehakte Items, die durch einen neuen `ChangeKey` oder Reason zurückkommen.
- **Öffnen des Flyouts** setzt „neu“ für das Icon zurück. Die Hervorhebung einzelner Items in der Liste bleibt bis „gesehen“/Klick.
- Overlay-Priorität: Fehler (rot) > Token-Ablauf bald (gelb) > neu (Akzent) > normal.
- Tooltip: Zähler je Quelle und Zeitpunkt der letzten Aktualisierung.
- Linksklick öffnet/schließt das Flyout. Ein Klick auf das Icon bei offenem Flyout löst zuerst den Fokusverlust (Flyout schließt) und dann den Klick aus; ein Klick innerhalb von ca. 300 ms nach dem Schließen durch Fokusverlust öffnet das Flyout deshalb nicht erneut.
- Rechtsklick öffnet das Kontextmenü:
  - Aktualisieren
  - Tokens verwalten…
  - Config öffnen (öffnet `config.json` im Standardeditor)
  - Config-Ordner öffnen
  - Log-Ordner öffnen
  - Autostart ☐
  - Beenden

### 8.2 Flyout

- Rahmenloses Fenster, verankert an der Tray-Position (Taskleiste unten/oben/links/rechts, Multi-Monitor, Per-Monitor-DPI v2). Es schließt sich bei Fokusverlust und mit Esc.
- Größe: ca. 420 × max. 640 px, Liste scrollbar.
- Folgt dem Hell-/Dunkelmodus von Windows.

**Kopfzeile**

- Zähler je Quelle als Filter-Chips (Klick filtert auf die Quelle, erneuter Klick hebt den Filter auf)
- Umschalter Ansicht „Gruppiert“ / „Liste“
- Umschalter Sortierung „Neueste zuerst“ / „Älteste zuerst“
- Aktualisieren-Button
- Beide Umschalter werden gespeichert.

**Ansichten**

- *Gruppiert:* Abschnitte Outlook / Slack / Jira / GitHub, einklappbar (Zustand wird gespeichert), Zähler je Abschnitt. Innerhalb einer Gruppe: gepinnt → Sortierung nach Umschalter.
- *Liste:* alles gemischt, gepinnt → Sortierung nach Umschalter. Die Quelle ist als Icon am Item erkennbar.

**Item-Zeile**

- Quellen-Icon, Titel (eine Zeile, gekürzt), Kontextzeile (Absender/Kanal/Ticket-Status/Repo), relative Zeit.
- Reason-Badges: „Flag“, „Ungelesen“, „Mention“, „DM“, „@here“, „Review“, „Assigned“, „Draft“, Name der Jira-Abfrage.
- Neue (nicht gesehene) Items haben einen Akzentpunkt und fetten Titel.
- Hover zeigt Aktionen: Pin, Gesehen, Abhaken. Der Klick auf die Zeile öffnet das Ziel gemäß Klickziel-Konfiguration.
- Tastatur: Pfeiltasten, Enter = öffnen, `P` = pinnen, `D`/Entf = abhaken, `S` = gesehen.

**Fußzeile:** Statuszeilen der Quellen (§7), „pausiert“-Hinweis, Link „Abgehakte anzeigen (N)“.

**Leerzustand:** „Nichts offen“ mit Zeit der letzten Aktualisierung. Ist noch keine Quelle konfiguriert, erscheint ein Hinweis mit Links „Config öffnen“ / „Tokens verwalten“.

### 8.3 Token-Dialog

Ein kleines Fenster mit einer Zeile je Quelle:

- Status (gesetzt/fehlt/ungültig), Feld zum Eintragen (maskiert), „Prüfen“-Button (ein Lese-Call: `auth.test`, `/rest/api/3/myself`, `GET /user`), „Entfernen“.
- Jira zusätzlich: optionales Ablaufdatum und der Hinweis, dass ein API-Token ohne Scopes nötig ist (§4.3).
- Outlook: kein Token-Feld, sondern „Anmelden“/„Abmelden“ (MSAL) mit Anzeige des angemeldeten Kontos.

Tokens werden nie angezeigt, nur ob sie gesetzt sind. Die Einträge im Credential Manager heißen `TaskHelper:slack`, `TaskHelper:jira`, `TaskHelper:github`.

## 9. Konfiguration

`%APPDATA%\TaskHelper\config.json`. Fehlt die Datei beim ersten Start, wird eine kommentierte Vorlage angelegt (JSON mit `ReadCommentHandling = Skip`). Ist ein Abschnitt nicht vorhanden oder `enabled: false`, ist die Quelle deaktiviert.

```jsonc
{
  "polling": {
    "defaultIntervalMinutes": 5,
    "workingHours": {
      "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
      "from": "07:30",
      "to": "18:30"
    },
    "pauseWhenLocked": true
  },
  "outlook": {
    "enabled": true,
    "tenantId": "<tenant-guid>",
    "clientId": "<app-client-id>",
    "ownAddresses": ["vorname.nachname@example.com"],
    "unread": {
      "focusedOnly": true,
      "toOnly": true,
      "excludeSenders": ["*@github.com", "jira@*.atlassian.net", "noreply@*"],
      "excludeSubjects": ["[JIRA]*"]
    },
    "openIn": "browser"            // "browser" | "desktop"
  },
  "slack": {
    "enabled": true,
    "channels": ["C0123456", "C0456789"],   // Team-ID kommt aus auth.test
    "directMessages": true,
    "maxUnreadPerChannel": 50,
    "openIn": "desktop"
  },
  "jira": {
    "enabled": true,
    "siteUrl": "https://example.atlassian.net",
    "email": "vorname.nachname@example.com",
    "queries": [
      { "name": "Review", "jql": "assignee = currentUser() AND status = \"In Review\"" },
      { "name": "Blockiert", "jql": "assignee = currentUser() AND status = Blocked" }
    ]
  },
  "github": {
    "enabled": true,
    "org": "example-org",
    "intervalMinutes": 5            // optional, überschreibt polling.defaultIntervalMinutes (geht bei jeder Quelle)
  },
  "logging": {
    "debug": false
  }
}
```

Validierung beim Laden: Pflichtfelder je aktivierter Quelle, URL-Format, Uhrzeiten, Intervalle ≥ 1. Fehler werden mit JSON-Pfad gemeldet.

## 10. Logging

- `%LOCALAPPDATA%\TaskHelper\logs\taskhelper-YYYYMMDD.log`, 7 Tage Rotation.
- Level Info: Poll-Start/-Ende je Quelle, Anzahl Items, Dauer, HTTP-Status bei Fehlern, Statuswechsel.
- Level Debug (Config-Schalter `logging.debug`): zusätzlich Request-URLs **ohne** Query-Werte, die Inhalte enthalten könnten, und ohne Auth-Header.
- Nie: Tokens, Mail-Betreffs, Nachrichtentexte, Ticket-Titel.

## 11. Tests und Entwicklung

- **Unit-Tests (Core):**
  - Mapping je Quelle anhand gespeicherter, anonymisierter JSON-Fixtures
  - Merge/Dedup (mehrere Reasons)
  - ChangeKey-/Abhaken-Logik (inkl. hinzukommender/wegfallender Reasons)
  - Lebenszyklus: Verschwinden nur bei vollständigem Ergebnis, Wiederauftauchen als neu, 30-Tage-Löschung relativ zu `LastSuccessAt`
  - „Neu“ für Tray-Icon (`LastChangeAt`) und Liste (`SeenChangeKey`)
  - Slack-Erledigt-Logik (Antwort im Thread vor/nach Mention, erneute Mention, gelöschte Nachricht)
  - Baseline und Suchfenster der Mention-Suche
  - Outlook-Filter (To, Ausschlussliste, Gelöschte Elemente/Junk)
  - Scheduler (Pause/Arbeitszeit/Backoff/`AuthRequired` mit fake Clock über `TimeProvider`)
  - Statuspriorität (§7)
  - Config-Validierung
- **HTTP** über einen Fake-`HttpMessageHandler`, kein echter Netzwerkverkehr in Tests.
- **Gegen echte Konten:** Nur lesende Calls und nur nach ausdrücklicher Rückfrage beim Benutzer im Einzelfall, z. B. für den Spike zur Slack-Query-Syntax oder zum Erfassen von Fixtures. Fixtures werden vor dem Commit anonymisiert.
- **Setup-Doku:** Je Quelle eine kurze Anleitung (`docs/setup-*.md`), wie Zugang und Token eingerichtet werden (Entra-App, Slack-App mit Scopes, GitHub-PAT mit Berechtigungen, Jira-API-Token ohne Scopes).
- Vor „fertig“ (gilt für jedes Issue): `dotnet build` ohne Warnungen (Nullable aktiviert, `TreatWarningsAsErrors`), `dotnet format --verify-no-changes`, `dotnet test`.

## 12. Vorbereitung auf firmenweite Verteilung

Nicht Teil von v1, aber bei Entwurfsentscheidungen zu berücksichtigen:

- Keine benutzer- oder firmenspezifischen Werte im Code; alles über Config. Die Config-Vorlage ist neutral.
- Entra-App: später eine zentrale Registrierung mit Admin-Consent statt einer persönlichen. Tenant-/Client-ID sind bereits Config-Werte.
- Slack: später eine vom Workspace-Admin freigegebene interne App, jeder Benutzer autorisiert per OAuth. Dafür wäre ein OAuth-Flow mit lokalem Redirect nötig, statt das Token manuell einzufügen. Der Token-Dialog ist so gebaut, dass ein „Mit Slack verbinden“-Button später ergänzt werden kann.
- Code-Signing der EXE (SmartScreen/AppLocker; kann schon für v1 nötig sein, siehe §13), Installer (z. B. MSI oder Intune-Paket), Update-Mechanismus.
- UI-Texte zentral als Ressourcen (Deutsch), damit eine Übersetzung möglich bleibt.
- Mehrere Jira-Sites/GitHub-Orgs: Das Datenmodell und die Config-Struktur sollen sich auf Listen erweitern lassen, ohne die Datenbank zu brechen. Der Item-Key enthält deshalb schon in v1 eine Instanzkennung (§3).

## 13. Risiken und offene Punkte

| # | Punkt | Umgang |
|---|---|---|
| 1 | Darf im Tenant eine eigene Entra-App registriert und `Mail.Read` per User-Consent erteilt werden? | Vor Implementierung klären; sonst Admin-Consent beantragen |
| 2 | Darf im Slack-Workspace eine interne App mit User-Token installiert werden? | Vor Implementierung klären |
| 3 | `search.messages`-Query-Syntax für Mentions der eigenen User-ID | Spike gegen echten Workspace (lesend, nach Rückfrage) |
| 4 | Anzahl der Calls für DMs bei vielen Konversationen | Spike messen; ggf. DMs mit `is_open=false` ausklammern |
| 5 | Outlook-Desktop-Deeplink über EntryID funktioniert nur mit klassischem Outlook | Default Browser, Desktop optional mit Fallback |
| 6 | Abhaken + `updated_at` kann zu häufigem Wiederauftauchen führen | Beobachten, ggf. ChangeKey pro Quelle verfeinern |
| 7 | Bestätigung der Dependencies MSAL (+ Broker/Extensions) und Testframework | **Geklärt (2026-11-13):** `Microsoft.Identity.Client`, `Microsoft.Identity.Client.Broker` und xUnit bestätigt |
| 8 | AppLocker/WDAC auf dem Firmenrechner blockiert evtl. schon in v1 eine unsignierte, selbst gebaute EXE im Benutzerprofil | Vor Projektsetup mit einer Test-EXE prüfen; sonst Code-Signing oder Freigabe vorziehen |
| 9 | Die GitHub-Org verlangt evtl. eine Freigabe für fine-grained PATs oder sperrt sie | **Geklärt (2026-11-13):** Zugriff auf die GitHub-API ist in bestehenden Werkzeugen bereits im Einsatz |
| 10 | Jira: Scoped API-Tokens laufen nur über `api.atlassian.com/ex/jira/{cloudId}`; v1 unterstützt nur Tokens ohne Scopes | **Geklärt (2026-11-13):** Zugriff auf die Jira-API ist in bestehenden Werkzeugen bereits im Einsatz |
| 11 | Slack-Desktop-Deeplink auf eine einzelne Nachricht ist nicht offiziell dokumentiert | Im Slack-Spike prüfen; Fallback https-Permalink |
| 12 | Outlook ist noch nicht praktisch erprobt: WAM-Anmeldung mit eigener App-Registrierung, Filter auf `inferenceClassification`, Verhalten bei Conditional Access | Machbarkeitstest vor dem Outlook-Adapter (lesend, nach Rückfrage) |

## 14. Umsetzungsreihenfolge (Vorschlag)

0. **Klärungen vorab, parallel zu Schritt 1:** Risiken 1, 2, 8 und 12 klären (7, 9 und 10 sind erledigt, GitHub und Jira gelten als machbar); Slack-Spike (Query-Syntax, Anzahl Calls für DMs, Deeplink; lesend, nach Rückfrage). Fällt eine Quelle weg oder ändert sich ihr Zugang, ändert sich der Zuschnitt. Slack und Outlook kommen bewusst zuletzt, weil sie noch nicht erprobt sind.
1. Projektgerüst (Core/App/Tests), Config laden + validieren, Logging, SQLite-Schema **inklusive** lokalem Zustand und Lebenszyklus-Feldern (§3), auch wenn die Aktionen erst in Schritt 7 kommen
2. Tray-Icon mit Zähler, Flyout mit statischen Daten, Ansicht/Sortierung umschaltbar
3. Credential-Manager + Token-Dialog
4. GitHub-Adapter (einfachste Quelle) → Ende-zu-Ende-Durchstich
5. Jira-Adapter
6. Scheduler mit Pausen, Fehlerzuständen, Token-Vorwarnung
7. Lokale Aktionen (Abhaken/Gesehen/Pinnen) inkl. ChangeKey-Logik
8. Outlook-Adapter (MSAL, Filter)
9. Slack-Adapter (Mentions → konfigurierte Kanäle → DMs) auf Basis des Spikes aus Schritt 0
10. Autostart, Einzelinstanz, Resume-Handling, Feinschliff UI (DPI, Dark Mode, Tastatur)
