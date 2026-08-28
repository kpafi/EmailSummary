# README-Entwurf — Abschnitt „Mirror-Postfach einrichten" (WP2)

> Entwurfstext für die spätere `README.md`. Die README selbst entsteht in WP9/WP12
> (ADR-009); dieser Abschnitt wird dort übernommen und ggf. an die endgültige
> CLI-Spezifikation (docs/SPEC-CLI.md) angepasst. Quelle: WP2, 2026-08-28.

## Mirror-Postfach einrichten

MailDigest liest **nie** dein echtes Postfach. Du legst ein zweites, dediziertes Postfach an
(das „Mirror-Postfach") und leitest deine Mails dorthin weiter. MailDigest bekommt nur die
Zugangsdaten dieses Zweitpostfachs — im schlimmsten Fall ist also nur eine Kopie deiner
Mails betroffen, nie dein Hauptkonto.

**Empfehlung:** Mirror-Postfach bei einem *anderen* Anbieter als dem Hauptpostfach, mit
einem eigenen, einmaligen Passwort. Wo möglich ein App-Passwort statt des Hauptpassworts.

### Schritt 1 — Mirror-Postfach anlegen

Ein leeres IMAP-Postfach bei einem Anbieter deiner Wahl. Notiere dir: IMAP-Host, Port
(fast immer 993), Benutzername, Passwort. **MailDigest verbindet ausschließlich per IMAPS
mit aktiver Zertifikatsprüfung** — Klartext-IMAP (Port 143) wird abgelehnt.

### Schritt 2 — Weiterleitung im echten Postfach einrichten

**Gmail:** Einstellungen → „Weiterleitung und POP/IMAP" → „Weiterleitungsadresse
hinzufügen" → Adresse des Mirror-Postfachs eintragen → Google schickt dorthin eine
Bestätigungsmail mit Code → Code eintragen. Danach „Eingehende Nachrichten weiterleiten an
…" aktivieren und wählen, ob Gmail die Kopie behalten soll (empfohlen: „Gmail-Kopie im
Posteingang behalten"). Für selektive Weiterleitung: Einstellungen → „Filter und blockierte
Adressen" → „Neuen Filter erstellen" → Bedingung wählen → „Weiterleiten an" ankreuzen.

**posteo:** Einstellungen → „E-Mail" → „Filterregeln" → neue Regel → Aktion „Weiterleiten
an" mit der Mirror-Adresse; optional „Nachricht zusätzlich im Postfach behalten" aktivieren.
posteo verlangt keine Bestätigung der Zieladresse.

**mailbox.org:** Einstellungen → „E-Mail" → „Filter" → neue Regel → Aktion „Umleiten nach"
mit der Mirror-Adresse, plus Aktion „Behalten", damit die Mail auch im Original bleibt.
Alternativ unter „Weiterleitung" eine pauschale Weiterleitung einrichten.

**Anderer Anbieter:** Gesucht ist eine serverseitige Weiterleitung oder Filterregel mit
Aktion „weiterleiten/umleiten an". Eine clientseitige Regel (Outlook/Thunderbird) reicht
nicht — die greift nur, wenn dein Rechner läuft.

> **Wichtig:** Richte keine Weiterleitung vom Mirror-Postfach zurück ins Hauptpostfach ein.
> Das erzeugt eine Schleife.

### Schritt 3 — MailDigest verbinden

```
maildigest connect-mail
```

fragt Host, Port, Benutzername und Passwort ab, testet die Verbindung und lässt dich den
Ordner wählen (Default `INBOX`). Das Passwort kannst du stattdessen auch über die
Umgebungsvariable `MAILDIGEST_IMAP_PASSWORD` setzen; dann steht es nicht in der Config-Datei.

### Was MailDigest im Mirror-Postfach tut — und was nicht

| | |
|---|---|
| Liest | ungelesene Mails im konfigurierten Ordner |
| Schreibt | das Gelesen-Flag nach der Verarbeitung; optional ein Verschieben in einen Ordner |
| Löscht | **nichts. Niemals.** Es gibt keinen Codepfad zum Löschen oder Leeren. |

Wenn du die verarbeiteten Mails aus dem Posteingang haben willst, setze in der `config.toml`:

```toml
[imap]
move_processed_to = "Processed"   # leer lassen = nur als gelesen markieren
```

Der Ordner muss auf dem Server bereits existieren.

### Abrufintervall

```toml
[imap]
poll_interval_seconds = 120   # Default: alle 2 Minuten
```

MailDigest pollt (kein IMAP IDLE) — eine neue Mail erscheint also bis zu einem Intervall
später im Messenger. Reißt die Verbindung ab, versucht MailDigest es mit wachsendem Abstand
erneut (5 s, 10 s, 20 s … maximal 10 Minuten) und läuft danach normal weiter.

---

## Vormerkung für WP9 (LLM-Setup, aus WP4)

Für den Abschnitt zu `connect-llm`: „Der Modellname ist Pflichtangabe, es gibt bewusst
keinen Default; den API-Key gibst du bevorzugt über `MAILDIGEST_LLM_API_KEY` an. Für lokale
Server (Ollama/vLLM/LM Studio) wählst du `provider = "openai_compatible"` und setzt
`base_url`; ein API-Key ist dort optional."
