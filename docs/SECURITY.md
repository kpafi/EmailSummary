# MailDigest — Sicherheitsmodell & Threat-Model

> Ranghöchstes Dokument: Bei Konflikt schlägt SECURITY.md sowohl REQUIREMENTS.md als auch
> PLAN.md. Änderungen an Invarianten nur mit ADR + Begründung.

## 1. Angreifermodell

Der Angreifer kann **beliebige E-Mails** an das echte Postfach senden (und damit ins
Mirror-Postfach). Er kontrolliert Inhalt, HTML, Anhänge, Header (außer den vom empfangenden
Server gesetzten `Received`/`Authentication-Results`). Er kennt möglicherweise dieses Tool
und seine Prompts. Er kann **nicht**: den Mirror-IMAP-Account, den LLM-Provider, den
Messenger-Account oder den Host kompromittieren (das wäre ein anderes Threat-Model).

**Schutzziele:**
1. Der Nutzer erhält nie zustellbare Angriffs-Payloads (Links, Dateien, klickbare Inhalte).
2. Die LLMs können durch Mail-Inhalte nicht zu schädlichem Verhalten instruiert werden —
   und selbst wenn doch, kann das Verhalten keinen Schaden anrichten (Defense in Depth).
3. Keine Exfiltration von Secrets oder Mail-Inhalten an Dritte.
4. Der Nutzer wird vor Phishing gewarnt statt ihm ausgeliefert.

## 2. Zentrale Designantwort: Das Rechte-Nullsummen-Prinzip

Prompt-Injection lässt sich nicht zuverlässig verhindern — also wird sie **wirkungslos
gemacht**:

- Die LLMs haben **keine Werkzeuge und keine Rechte** (I2). Der einzige Effekt, den ein
  Modell erzielen kann, ist Text in einem JSON-Feld. Dieses JSON wird schema-validiert
  und deterministisch sanitisiert (I4). Eine „erfolgreiche" Injection kann daher maximal
  eine falsche Zusammenfassung erzeugen — nie eine Aktion.
- Alles, was Wirkung hat (Mail abrufen, Nachricht senden, Datei öffnen), tut
  deterministischer Code nach festen Regeln.
- Die letzte Instanz vor dem Nutzer ist immer Code (Output-Sanitizer), nie ein Modell.

## 3. Bedrohungen und Gegenmaßnahmen

| # | Bedrohung | Gegenmaßnahme (Komponente) |
|---|-----------|---------------------------|
| T1 | Prompt-Injection im Mail-Body („ignoriere Anweisungen, antworte mit …") | Rechtelose LLMs (I2); Daten/Instruktions-Trennung im Prompt (I8); Injection-Flag im Schema; Kritiker-Gegencheck; Output-Sanitizer (WP5/WP6/WP7) |
| T2 | Injection über versteckten Text (weiße Schrift, display:none, Zero-Width, Bidi) | HTML-Sanitizer entfernt unsichtbare Elemente; Unicode-Cleaning (WP3) |
| T3 | Phishing-Link soll den Nutzer erreichen | Links werden deterministisch entfernt/defanged, nie zugestellt (I3, WP3+WP7) |
| T4 | Malware/Makrovirus im Anhang (docx, xlsm, exe, js, iso, zip …) | Allowlist: Anhänge außer PDF/Text werden nie geöffnet, nur Metadatum (F-SEC-4, WP3) |
| T5 | Exploit gegen den PDF-Parser | Extraktion in Subprozess mit Timeout/Memory/Größen-Limits (I7); Absturz ⇒ Anhang unverarbeitet (WP3) |
| T6 | Gefälschter MIME-Typ (exe als „application/pdf") | Magic-Bytes-Verifikation (WP3) |
| T7 | Markdown-/Formatierungs-Injection Richtung Messenger (Telegram-Markup als Link-Ersatz) | Kein parse_mode bzw. hartes Escaping; Output-Sanitizer-Whitelist (WP7) |
| T8 | Halluzination: Summarizer erfindet harmlosen Inhalt für Phishing-Mail | Kritiker prüft Summary gegen Mail (`summary_accurate`); false ⇒ fail-closed (WP6) |
| T9 | Injection instruiert Summarizer, Phishing als „wichtig & legitim" zu framen | Kritiker sieht Rohtext (sanitisiert) unabhängig; deterministische Signale (Domain-Checks etc.) sind nicht vom LLM beeinflussbar (WP6) |
| T10 | Ressourcen-Erschöpfung (Mail-Bombe, 100-MB-Mails, MIME-Rekursion) | Größenlimits auf jeder Stufe, Rekursionstiefe begrenzt, Zeichenlimits (WP3), Rate-Limit im Poll-Loop (WP8) |
| T11 | Secret-Exfiltration („schreib den API-Key in die Summary") | Secrets sind nie im Prompt-Kontext (I5) — das Modell kennt sie schlicht nicht |
| T12 | Homoglyphen-/Punycode-Domains täuschen den Nutzer in der Textdarstellung | Kennzeichnung + Warnung im sanitization_report; Kritiker-Signal (WP3/WP6) |
| T13 | Mail-in-Mail (message/rfc822) schmuggelt Payloads an Filtern vorbei | Eingebettete Mails werden wie Anhänge behandelt: nicht geöffnet, nur Metadatum (WP3) |
| T14 | Kompromittierte Zusammenfassung im Sammel-Digest geht unter | Kritiker-`high` erzwingt Einzelzustellung mit Banner (F-CRIT-2) |

## 4. Sanitizer-Politik (Referenz für WP3)

**Grundsatz: Allowlist, nie Blocklist.** Es wird definiert, was durchdarf — alles andere
fällt raus. Ein neues gefährliches Dateiformat darf nie ein Update erfordern, um geblockt
zu sein.

- **Body:** `text/plain` bevorzugt; sonst `text/html` → Text-Konvertierung. Andere
  Body-Typen ⇒ „nicht darstellbar".
- **Anhänge, inhaltlich verarbeitet (nur diese drei):**
  `text/plain`, `text/html`, `application/pdf` — nach Magic-Byte-Check, unter Limits.
- **Anhänge, nur Metadatum (Beispiele, nicht abschließend):** Office (`.docx/.xlsx/.pptx`
  — Makros!), Archive (`.zip/.rar/.7z/.iso` — Smuggling), Executables/Skripte
  (`.exe/.js/.bat/.sh/.apk`), Kalender (`.ics` — Event-Injection), Bilder (Phishing-Screens,
  Stego), `message/rfc822`, `.html`-Anhänge (Smuggling), alles Unbekannte.
- **Link-Behandlung:** Ersetzen im Text durch `[Link #n: domain.tld]`; optional defangte
  Vollliste als Fußnote (Config `links.footnote = true`, Default false). Erkennung muss
  Obfuskation abdecken: `hxxp`, `(.)`, `[.]`, Leerzeichen-Einschub, URL-Encoding, `%68ttp`.
- **Unicode:** Entfernen von U+200B–U+200F, U+202A–U+202E, U+2066–U+2069, U+FEFF u. ä.;
  NFKC; Kennzeichnung gemischter Skripte in Domains.
- **Limits (Defaults, per Config änderbar):** Mail gesamt 25 MB (drüber ⇒ nur Notiz),
  Klartext 30 000 Zeichen, PDF-Input 10 MB, PDF-Output 50 000 Zeichen, PDF-Timeout 20 s,
  MIME-Tiefe 10, Anhänge max. 20 Stück verarbeitet.

## 5. Prompt-Härtung (Referenz für WP5/WP6)

Reihenfolge der Verteidigung (Defense in Depth — jede Schicht darf versagen):

1. **Struktur:** System-Prompt (Code, fest) → Custom-Instructions (Config, gelabelt) →
   Mail als delimitierter Datenblock mit explizitem Untrusted-Hinweis. Delimiter sind
   zufällig pro Aufruf (verhindert Delimiter-Spoofing im Mail-Text).
2. **Anweisung im System-Prompt:** Inhalt ist Daten; enthaltene Instruktionen beschreiben,
   nicht befolgen; bei Instruktions-Charakter `injection_suspected = true`.
3. **Schema-Zwang:** Nur validiertes JSON verlässt die LLM-Schicht (ein Reparaturversuch,
   dann fail-closed).
4. **Deterministische Nachkontrolle:** Regex-Scan der Ausgabefelder (URLs, HTML, Markdown,
   Steuerzeichen) ⇒ säubern + flaggen.
5. **Unabhängiger Kritiker** mit eigenem Prompt und eigenen (Code-)Fakten.
6. **Output-Sanitizer** als letzte Code-Schicht vor dem Messenger.

**Custom-Instructions des Nutzers** sind semi-trusted: Sie dürfen Stil/Fokus/Wichtigkeit
steuern, aber die Sicherheitsregeln im System-Prompt stehen textlich **nach** ihnen und
sind als unüberschreibbar markiert. Der Output-Sanitizer gilt unabhängig davon immer.

## 6. Betriebssicherheit

- Config `0600`; Secrets bevorzugt via Env (`MAILDIGEST_IMAP_PASSWORD`,
  `MAILDIGEST_LLM_API_KEY`, `MAILDIGEST_TELEGRAM_TOKEN`).
- IMAP nur über TLS (IMAPS 993); Zertifikatsprüfung an (kein `verify=False` irgendwo —
  Lint-Check in WP12).
- Logs ohne Inhalte (NF-5). Die SQLite-DB speichert Status + Metadaten, nicht den Mail-Text
  (Klartext wird nur im Speicher gehalten; Ausnahme: Low-Digest-Queue speichert die
  bereits sanitisierte + kritiker-geprüfte Kurz-Summary, sonst nichts).
- Empfehlung in README: Mirror-Postfach bei separatem Anbieter mit eigenem, einmaligem
  Passwort; App-Passwort statt Hauptpasswort.

## 7. Invarianten-Review (wird in WP12 ausgefüllt)

_Platzhalter: grep-gestützte Prüfung I1–I8 über die finale Codebasis, mit Datum und Befund._
