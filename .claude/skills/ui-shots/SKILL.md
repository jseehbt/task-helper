---
name: ui-shots
description: Starts the TaskHelper WPF app on the Windows desktop in demo mode with static items, captures its tray flyout, token dialog and other windows as PNG screenshots in light and dark colour mode and at the current display scaling, then has the ui-reviewer agent judge them against SPEC §8. Use it after every change to XAML, styles, the flyout, the tray icon, the token dialog, theming, DPI or keyboard handling - build and unit tests say nothing about how the flyout looks - and when someone asks how the UI looks now, even if they only say "zeig mal das Flyout" or "passt der Dark Mode". Not for checking logic without a visible effect, not for screenshots of other applications, and not on a machine without a Windows desktop session.
---

# UI Screenshots

## Goal

Screenshots of every window the change affects, in light and dark mode, were looked at by the main session and by the `ui-reviewer` agent, and
no Critical or Major UI finding is open - or the user knows that the visual check is missing and why.

## Workflow

`S` = `uv run --no-project python .claude/skills/ui-shots/scripts/shots.py`; run from the repository root.

1. **Build.** `dotnet build` (the `test-runner` agent does it). The script picks the newest `TaskHelper*.exe` under `TaskHelper.App/bin/`.
2. **Tell the user first** that the Windows app colour mode switches briefly to light and dark and is restored afterwards - it affects every open
   app for a few seconds.
3. **Capture.** `S` (defaults: `--themes light,dark --app-args "--demo --show-flyout"`). For the token dialog or another window pass the
   app's switch for it in `--app-args`. Each `shot=` line names file, theme, DPI and window size; screenshots go to a temp folder, not into the
   repository.
   - Exit 5 `app_exited`/`no_window`: the app does not fulfil the demo contract in AGENTS.md yet (`--demo`, `--show-flyout`, own state and
     single-instance name). Say so; do not capture the real instance with real data instead.
   - `warning=blank_capture`: PrintWindow got no content; report it as "visuell nicht geprüft", do not treat a black image as a finding.
4. **DPI.** The script records the DPI of each window but cannot change the scaling. SPEC §8.1 and issue 21 ask for at least two levels: ask the
   user to switch the display scaling (e.g. 100 % and 150 %) and run step 3 again for the second level.
5. **Look yourself.** Open at least the flyout in both themes with Read. Ask: does everything follow the theme (header, chips, rows, badges,
   footer, scrollbar), is text readable, is the "new" accent visible, does the counter badge fit at "99+", is anything clipped at this DPI?
6. **Review.** Start the `ui-reviewer` agent with the screenshot paths, the changed files, the issue text and SPEC §8. Fix Critical and Major
   findings, then step 1-6 again, at most two rounds.

## References and Scripts

* Run `scripts/shots.py [--exe PATH] [--out DIR] [--themes light,dark|current] [--app-args "…"] [--settle SECONDS]`; Windows only. Exit 2
  usage or no exe, 4 not Windows, 5 app exited early or showed no window.
* AGENTS.md, "App-Vertrag für ui-shots" - the switches the app has to support for this skill.

## Safety

* Only the demo mode: a capture of the real instance shows real subjects and messages, which must not end up in the conversation or a file.
* The script restores the colour mode in a `finally`; if it was killed midway, tell the user to check Settings → Personalisation → Colours.

## Pitfalls

* The flyout closes on focus loss (SPEC §8.2); without `--show-flyout` keeping it open in demo mode, the capture shows an empty desktop area.

## Done When

* [ ] Each affected window has a screenshot in light and dark mode at two DPI levels, or the user knows which ones are missing and why.
* [ ] The main session looked at the flyout screenshots itself; `ui-reviewer` reports no open Critical or Major finding.
