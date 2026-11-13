# AGENTS.md

Hinweise für Coding-Agents (Claude Code und andere) in diesem Repository.

## Projekt

TaskHelper ist eine Windows-Tray-App (.NET 10, WPF). Sie zeigt offene Aufgaben aus Outlook, Slack, Jira Cloud und GitHub an einer Stelle.
Maßgeblich ist [SPEC.md](SPEC.md). Die Umsetzung folgt den GitHub Issues. Die Reihenfolge steht in Issue #22 (SPEC §14).

- `TaskHelper.Core` (`net10.0`): Modell, Adapter, Scheduler, Persistenz, Config. Keine WPF-/WinForms-Referenzen.
- `TaskHelper.App` (`net10.0-windows`, WPF): Tray, Flyout, Token-Dialog, Credential Manager, Session-Events, Autostart.
- `TaskHelper.Core.Tests`: Unit-Tests mit HTTP-Fixtures über einen Fake-`HttpMessageHandler` und Zeit über einen Fake-`TimeProvider`.

Entwickelt wird unter Windows. Python-Skripte der Skills und Hooks laufen über `uv run --no-project python`, nicht über `python3`.

## Grundregeln

- **Strikt lesend** gegenüber allen Quellen. Kein Als-gelesen-Markieren, keine Reaktion, keine Transition. Die einzige Ausnahme ist Graph
  `translateExchangeIds` (POST, ändert nichts).
- **Echte Konten** werden nur lesend angesprochen und nur nach ausdrücklicher Rückfrage beim Nutzer, jedes Mal (SPEC §11). Das gilt auch für
  Fixtures, siehe Skill `capture-fixture`.
- **Fixtures** sind anonymisiert. `.claude/fixture-denylist.txt` ist lokal und nicht eingecheckt. Darin stehen die echten Org-, Site-, Workspace-
  und Personennamen, die nie im Repo landen dürfen.
- **Logs** enthalten keine Inhalte und keine Tokens (SPEC §10).
- **Nichts Spezifisches im Code:** keine Tenant-, Client-, Org- oder Site-IDs, keine Adressen. Alles kommt aus der Config (SPEC §12).
- **Dependencies:** Keine neuen NuGet-Pakete ohne Rückfrage. SPEC §2 listet, welche Pakete bestätigt sind.
- **Git:** Nie auf `main` committen. Die Branches heißen `<issue>-<slug>`. Commits folgen Conventional Commits mit Issue-Präfix, z. B.
  `#11 feat: …`, ohne Co-Author- oder Tool-Footer. Push und PR nur nach Rückfrage.

## Build und Test

Definition of Done für **jedes** Issue (SPEC §11):

```bash
dotnet build                               # ohne Warnungen (Nullable, TreatWarningsAsErrors)
dotnet format --verify-no-changes
dotnet test
```

Die Solution-Datei und das Publish-Profil entstehen mit Issue #4. Danach hier den Publish-Befehl ergänzen.

Für die Agent-Werkzeuge gilt:

```bash
uvx ruff check .claude
uv run --no-project python -m unittest discover -s .claude/tests
```

## App-Vertrag für ui-shots

Damit der Skill `ui-shots` Screenshots machen kann, unterstützt `TaskHelper.App` diese Startparameter (umzusetzen mit #9, Einzelinstanz #20):

- `--demo`: statische Beispiel-Items, kein Netzwerkzugriff, eigenes Zustandsverzeichnis statt `%LOCALAPPDATA%\TaskHelper`. Außerdem ein eigener
  Mutex-/Pipe-Name, damit die Demo-Instanz nicht an eine laufende echte Instanz übergibt.
- `--show-flyout`: öffnet das Flyout beim Start. Im Demo-Modus bleibt es bei Fokusverlust offen.
- `--show-token-dialog`: öffnet den Token-Dialog beim Start (#10).

## Agents und Skills

Projekt-Skills liegen direkt in [.claude/skills/](.claude/skills/), ohne Symlink. Agents liegen in [.claude/agents/](.claude/agents/). Reviewer
melden nur, die Hauptsession ändert den Code.

### Skills

| Skill | Wozu |
|---|---|
| `implement-issue` | Setzt ein Issue um: prüft Abhängigkeiten, lässt es bewerten, legt den Branch an, setzt um, prüft, reviewt und committet. Danach hakt es die Kriterien ab und kommentiert das Issue. Ohne Nummer nimmt es das nächste freie Issue. |
| `add-source-adapter` | Checkliste und Abdeckungsskript für Quellen-Adapter (GitHub, Jira, Outlook, Slack) |
| `capture-fixture` | Fixtures von Hand schreiben oder mit einem freigegebenen GET erfassen, anonymisieren und auf Leaks prüfen |
| `review-branch` | Verteilt die geänderten Dateien auf die Reviewer, höchstens drei Runden Review und Fix |
| `spec-sync` | Gleicht SPEC.md, AGENTS.md und `docs/setup-*.md` mit dem Code ab und nennt betroffene offene Issues |
| `ui-shots` | Demo-App starten, Screenshots hell/dunkel, Bewertung durch `ui-reviewer` |

### Agents

| Agent | Wozu |
|---|---|
| `requirements-reviewer` | Ist ein Issue so umsetzbar? Urteil auf Deutsch mit offenen Fragen |
| `spec-reviewer` | Code gegen die Fachregeln von SPEC §3–§7 und die Grundregeln |
| `dotnet-reviewer` | C#-, WPF-, Test- und Build-Qualität |
| `docs-reviewer` | Ein Dokument gegen den Code, liefert exakte Änderungen |
| `ui-reviewer` | Screenshots gegen SPEC §8 |
| `api-researcher` | Verhalten der Slack-, Graph-, Jira- und GitHub-API mit Quellen. Merkt sich Ergebnisse in `.claude/agent-memory/api-researcher/` |
| `test-runner` | Build, Format und Tests ausführen und knapp berichten |

### Hooks

Die Hooks sind in `.claude/settings.json` eingetragen, die Skripte liegen in [.claude/hooks/](.claude/hooks/):

- `dotnet_format_check.py` (nach Edit/Write einer `.cs`-Datei): meldet Whitespace-Formatfehler und ändert nichts.
- `checks_on_stop.py` (Stop): einmal je Änderungsstand `dotnet build` + `dotnet test` und die Tests der Agent-Skripte.
- `guard_commit.py` (vor `git commit`): blockiert Commits auf `main`, Co-Author-Footer, Tokens und Namen aus der Denylist sowie echte E-Mails
  und Tenant-Hosts in Fixtures.
- Zusätzlich fragen `ask`-Regeln vor `curl`/`wget` und vor WebFetch auf die API-Hosts nach.

### Passende globale Skills

- `hbt-skill-creator`: Projekt-Skills ändern. `check_skill.py --strict` muss für `.claude/skills` grün sein.
- `code-review` und `security-review` ergänzen `review-branch`.
- `hbt-github-action-creator`: für eine spätere CI.
