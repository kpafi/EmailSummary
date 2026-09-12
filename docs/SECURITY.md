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
| T1 | Prompt-Injection im Mail-Body („ignoriere Anweisungen, antworte mit …") | Rechtelose LLMs (I2); Daten/Instruktions-Trennung im Prompt (I8); Injection-Flag im Schema; Kritiker-Gegencheck; Output-Sanitizer (WP5/WP6/WP7); deterministische Injection-Indizien in der WP5-Nachkontrolle, modellunabhängig (WP11, ADR-061) |
| T2 | Injection über versteckten Text (weiße Schrift, display:none, Zero-Width, Bidi) | HTML-Sanitizer entfernt unsichtbare Elemente; Unicode-Cleaning (WP3) |
| T3 | Phishing-Link soll den Nutzer erreichen | Links werden deterministisch entfernt/defanged, nie zugestellt (I3, WP3+WP7) |
| T4 | Malware/Makrovirus im Anhang (docx, xlsm, exe, js, iso, zip …) | Allowlist: Anhänge außer PDF/Text werden nie geöffnet, nur Metadatum (F-SEC-4, WP3) |
| T5 | Exploit gegen den PDF-Parser | Extraktion in Subprozess mit Timeout/Memory/Größen-Limits (I7); Absturz ⇒ Anhang unverarbeitet (WP3) |
| T6 | Gefälschter MIME-Typ (exe als „application/pdf") | Magic-Bytes-Verifikation (WP3) |
| T7 | Markdown-/Formatierungs-Injection Richtung Messenger (Telegram-Markup als Link-Ersatz) | Kein `parse_mode` (Telegram), nur `content` ohne Embeds (Discord); Output-Sanitizer löscht Markup-Zeichen und bricht alle Domain-Punkte sowie jedes lebende Schema (WP7, ADR-036/037) |
| T8 | Halluzination: Summarizer erfindet harmlosen Inhalt für Phishing-Mail | Kritiker prüft Summary gegen Mail (`summary_accurate`); false ⇒ fail-closed (umgesetzt: WP6 liefert das Feld, `pipeline.process_mail` zieht die Konsequenz) |
| T9 | Injection instruiert Summarizer, Phishing als „wichtig & legitim" zu framen | Kritiker sieht den sanitisierten Text unabhängig und ohne Custom-Instructions (ADR-042); deterministische Signale (Domain-Checks etc.) sind nicht vom LLM beeinflussbar und heben die Risikostufe notfalls im Code an (WP6, ADR-043) |
| T10 | Ressourcen-Erschöpfung (Mail-Bombe, 100-MB-Mails, MIME-Rekursion, tief geschachteltes HTML) | Größenlimits auf jeder Stufe, Rekursionstiefe begrenzt, Zeichenlimits (WP3), Byte-, Element- und Tiefenschranke der HTML→Text-Konvertierung als Restbudget je Mail (ADR-084: darüber gilt der HTML-Teil als nicht verarbeitet), Roh-Budget für Mail- und Anhangstext **vor** der Sanitisierung als Restzähler je Mail und ein Budget für die Zahl der Link-Funde je Mail (ADR-084-/ADR-028-Nachtrag), Obergrenze für die Zahl der gelaufenen MIME-Teile (500) und ein Zeitbudget aller PDF-Extraktionen einer Mail (ADR-084-/ADR-029-Nachtrag, vierte Iteration), Obergrenze für den Rohwert **jedes** gelesenen Headers vor jeder Verarbeitung (4096 Zeichen, 32 Werte je Name) sowie ein Kopfzeilen-Gesamtbudget des MIME-Baums vor der Rück-Serialisierung (256 KiB, 4096 Kopfzeilen; weitere Teile werden abgeschnitten — ADR-020-Nachtrag, fünfte Iteration), Rate-Limit im Poll-Loop (WP8) |
| T11 | Secret-Exfiltration („schreib den API-Key in die Summary") | Secrets sind nie im Prompt-Kontext (I5) — das Modell kennt sie schlicht nicht |
| T12 | Homoglyphen-/Punycode-Domains täuschen den Nutzer in der Textdarstellung | Kennzeichnung + Warnung im sanitization_report; Kritiker-Signal (WP3/WP6) |
| T13 | Mail-in-Mail (message/rfc822) schmuggelt Payloads an Filtern vorbei | Eingebettete Mails werden wie Anhänge behandelt: nicht geöffnet, nur Metadatum (WP3) |
| T14 | Kompromittierte Zusammenfassung im Sammel-Digest geht unter | Kritiker-`high` erzwingt Einzelzustellung mit Banner (F-CRIT-2) |
| T15 | `multipart/alternative`: harmloser `text/plain`, bösartiger `text/html` — der Nutzer sieht den HTML-Teil, die Zusammenfassung beschreibt den Klartext | Divergenz-Erkennung im Sanitizer setzt `html_divergent`; Hinweiszeile + Kritiker-Signal (WP11, ADR-067) |
| T16 | Gefälschte `Message-ID` unterdrückt eine echte Mail: Der Angreifer schickt seine Mail zuerst mit der Message-ID der erwarteten Mail; die echte gilt als Duplikat und verschwindet ohne Zustellung, Notiz oder Warnung | Zweites, inhaltsabgeleitetes Dedupe-Merkmal `content_hash` (`sha256(mime_bytes)`) in `seen_mails`; gleicher Key bei anderem Inhalt ist eine **Kollision**: Die Mail wird unter einem abgeleiteten Schlüssel regulär verarbeitet, bekommt eine Hinweiszeile und erzeugt `mail_id_collision` (WARNING) im Log (Fixrunde, HC-10, ADR-079) |

## 4. Sanitizer-Politik (verbindlich; umgesetzt in WP3, ADR-026 bis ADR-030)

**Grundsatz: Allowlist, nie Blocklist.** Es wird definiert, was durchdarf — alles andere
fällt raus. Ein neues gefährliches Dateiformat darf nie ein Update erfordern, um geblockt
zu sein.

- **Body:** Existiert mindestens ein Inline-`text/plain`-Teil, bilden **alle**
  Inline-`text/plain`-Teile (in MIME-Reihenfolge) den Body; sonst werden alle
  Inline-`text/html`-Teile konvertiert. Andere Body-Typen ⇒ „nicht darstellbar"
  (Anhang-Metadatum). „Inline" heißt: `Content-Disposition` ist nicht `attachment`
  **und** kein Dateiname gesetzt. Existieren beide Sorten (`multipart/alternative`), wird der
  HTML-Teil weiterhin **nicht** ausgewertet, aber intern zu Text konvertiert und mit dem
  Klartext verglichen (Wortmengen, Link-/Bild-Marker ausgenommen). Ein substanzieller
  Überhang (≥ 5 im Klartext fehlende Wörter **und** > 50 % der HTML-Wörter) setzt
  `sanitization_report.html_divergent` (T15, ADR-067).
- **Anhänge, inhaltlich verarbeitet (nur diese zwei Fälle):** `text/plain`-Dateien und
  `application/pdf` — nach Magic-Byte-Check, unter Limits. `text/html` ist **nur als
  Inline-Body** erlaubt; eine `.html`-*Datei* ist HTML-Smuggling-Vektor und bleibt
  Metadatum (`detected_kind="html"`, `processed=False`).
- **Magic-Bytes-Verifikation (T6, ADR-026):** Dem Header-MIME wird nie geglaubt.
  Verarbeitet wird nur, wenn der deklarierte Typ auf der Allowlist steht **und** der
  Inhalt dazu passt: PDF ⇒ `%PDF-` exakt an Offset 0; text ⇒ keine bekannte
  Binärsignatur (eigene kleine Tabelle in `sanitize/attachments.py`: MZ/ELF/Mach-O, ZIP/
  RAR/7z/GZIP/BZIP2/XZ/CAB, OLE2, PNG/JPEG/GIF/BMP/TIFF, RTF, `#!`-Skripte, SQLite,
  WASM, PDF) und Text-Heuristik bestanden (kein NUL, < 5 % Steuerbytes in 8 KB
  Stichprobe). Widerspruch ⇒ `detected_kind="mismatch"` ⇒ nie verarbeitet. Ein
  nicht-allowgelisteter Typ wird nie „hochgestuft", auch wenn sein Inhalt wie PDF aussieht.
- **Anhänge, nur Metadatum (Beispiele, nicht abschließend):** Office (`.docx/.xlsx/.pptx`
  — Makros!), Archive (`.zip/.rar/.7z/.iso` — Smuggling), Executables/Skripte
  (`.exe/.js/.bat/.sh/.apk`), Kalender (`.ics` — Event-Injection), Bilder (Phishing-Screens,
  Stego), `message/rfc822` (T13: wird **nie** betreten, auch nicht rekursiv),
  `.html`-Anhänge (Smuggling), alles Unbekannte. Erfasst werden sanitisierter Dateiname
  (ASCII-Allowlist, Pfadanteile entfernt, max. 80 Zeichen), deklarierter MIME-Typ, Größe.
- **Verschlüsselte Mail (PGP/S-MIME, ADR-082):** `multipart/encrypted`,
  `application/pgp-encrypted`, `application/pkcs7-mime` und `application/x-pkcs7-mime`
  werden **erkannt und benannt**, nicht entschlüsselt. Sicherheitlich folgt daraus nichts
  Neues: Der Chiffretext steht nicht auf der Allowlist und ist damit ohnehin nur
  Metadatum — kein Modell sieht ihn. Das Flag `SanitizationReport.encrypted` dient
  ausschließlich der Erklärung gegenüber dem Nutzer (Hinweiszeile) und dem Kritiker
  (weiches Signal, kein Risiko-Aufschlag). Kein Fail-closed: Bei verschlüsselter Mail ist
  nichts schiefgegangen, es ist nur nichts zu lesen.
- **Link-Behandlung (I3/T3, ADR-028):** Ersetzen im Text durch `[Link #n: domain.tld]`
  (`mailto:` ⇒ `[Mail #n: domain]`, `tel:` ⇒ `[Tel #n]`); die defangte Vollliste steht
  immer in `links_found` (`hxxps[:]//evil[.]com/…`, max. 100 Einträge à 300 Zeichen) und
  wird nur bei `links.footnote = true` (Default false) zusätzlich als Fußnote an den
  Body angehängt. Erkennung deckt Obfuskation ab: `hxxp`, `(.)`, `[.]`, `(dot)`,
  Leerzeichen-Einschub, URL-Encoding (`%68ttp`), `www.`-Domains, nackte Domains
  (eng gesetzte Punkte, alphabetische TLD; bekannte Datei-Endungen ausgenommen),
  Userinfo-Tricks (`http://gut@boese/` ⇒ Domain ist `boese`). Ein `www.`-Präfix wird im
  Marker gestrippt (Autolink-Gefahr im Messenger).
- **Punycode/Homoglyphen (T12):** `xn--`-Domains werden im Marker gekennzeichnet und in
  `punycode_domains` inkl. Unicode-Darstellung gelistet; Labels mit gemischten
  Schriftsystemen landen in `mixed_script_domains`. Beides gilt auch für die
  Absender-Domain.
- **Unicode (F-SEC-10):** NFKC-Normalisierung; danach wird **jedes** Zeichen der
  Unicode-Kategorie „C*" außer Tab/Zeilenumbruch entfernt und gezählt (deckt
  U+200B..200F, U+202A..202E, U+2066..2069, U+FEFF, U+00AD, U+2060..2064 u. ä. ab —
  bewusst kategorienbasiert statt Codepunkt-Blockliste). Gilt für Body, Anhangs-Texte,
  Betreff und Absender-Anzeigename.
- **HTML → Text (T2, ADR-027):** `script`/`style`/`head`/`template`/`noscript`/`iframe`/
  `object`/`embed`/`svg`/`math` und Kommentare werden entfernt; unsichtbarer Text
  (`display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, weiße Schrift ohne
  eigenen nicht-weißen Hintergrund, `hidden`-Attribut) wird entfernt und im Report
  gezählt (`hidden_text_removed`); Tracking-Pixel (≤ 2×2 px) werden ersatzlos entfernt;
  Alt-Texte erscheinen als `[Bild: …]`; `href`-Ziele werden als Text sichtbar gemacht und
  dann defangt. Zusätzlich werden HTML-Tag-artige Sequenzen auch in *Klartext*-Teilen
  neutralisiert (fail-safe: lieber Über-Entfernung als ein Tag im Output). Die Konvertierung
  hat eine harte Schranke (T10/ADR-084): mehr als `[limits] max_html_bytes` Bytes, mehr als
  `[limits] max_html_elements` Elemente oder mehr als 2000 Schachtelungsebenen ⇒ der
  HTML-Teil gilt als **nicht verarbeitet** (`html_rejected` im Report, Hinweiszeile in der
  Nachricht); ein `text/plain`-Teil wird normal zugestellt, die Pipeline läuft fail-safe
  weiter. Der Byte-Deckel greift **vor** dem Parsen (Element- und Tiefenschranke kennen den
  Baum erst danach, der Parse trägt die Kosten); Byte- und Elementschranke sind ein
  Restbudget **je Mail**, und höchstens vier `text/html`-Teile werden überhaupt konvertiert
  (ADR-084-Nachtrag). Derselbe Deckel gilt für den Divergenzcheck (ADR-067).
- **Absender-Anzeigename (ADR-020 Nachträge):** Adresse und `from_domain` werden aus dem
  **rohen** `From`/`Reply-To`-Wert geparst; RFC-2047 wird nur auf den Namensteil angewandt,
  der danach von `<`, `>`, `,`, `;`, `:`, `"`, `\` befreit wird. Ein Anzeigename kann die
  Absender-Domain damit weder ersetzen noch löschen, und die deterministischen Indikatoren
  (`reply_to_mismatch`, `return_path_mismatch`) bleiben wirksam (HC2-2). Kodierte Wörter
  werden vor dem Adress-Parse maskiert; die Maske folgt der Form von `email.header.ecre` und
  ist damit nie enger als der Dekoder, der danach läuft (ADR-020-Nachtrag, dritte Iteration),
  sucht das Ende eines kodierten Wortes aber mit `str.find` statt mit einem `.*?` — sonst ist
  sie quadratisch in der Zahl kaputter Wörter (R-8). Der **rohe** Wert **jedes** gelesenen
  Headers wird an einer einzigen Stelle (`_raw_header_values`) auf 4096 Zeichen geschnitten
  — nicht nur `From`/`Reply-To`/`Subject`, auch `To`, `Cc`, `Message-ID`, `Date`,
  `Return-Path`, `Authentication-Results` (S-1; ein 20-MB-`To` kostete vorher 18,5 s), und
  der geparste Baum wird vor der Rück-Serialisierung als Ganzes gedeckelt (256 KiB und 4096
  Kopfzeilen je Mail; der Falter der Standardbibliothek kostete sonst bis 14 s). Verwirft
  `getaddresses` den Adressteil ganz (ein Token hinter der spitzen Klammer genügt), greift
  ein zweiter, konservativer Schritt auf die **erste** spitze Klammer zurück — der
  Anzeigename darf die Domain auch nicht *löschen* (R-9). **Rückfälle lesen nie Kommentar-
  oder Quoted-String-Inhalt** (S-2): Ein linearer Scanner nach RFC 5322 entfernt `(…)` und
  `"…"` (mit Escapes und Verschachtelung), bevor gesucht wird; ist der Header unbalanciert
  (offener Kommentar oder Quoted String, auch durch den 4096-Schnitt), gilt die Adresse als
  unbekannt und die Rückweg-Warnung feuert. Orakel jedes Tests ist `email.policy.default`
  auf demselben Rohheader; das Werkzeug weicht davon nur nach „echte Angreiferadresse"
  oder „unbekannt + Warnung" ab, nie zu einer fremden Domain.
- **PDF-Extraktion (I7/T5, ADR-029):** `pdfminer.six` läuft ausschließlich in einem
  Subprozess (`python -m maildigest.sanitize.extract_pdf`, PDF via stdin), mit Timeout
  (Eltern-Prozess), `RLIMIT_AS` 512 MB (Code-Konstante) und Output-Kürzung im Kind.
  stderr wird verworfen (I5). Jeder Fehler ⇒ Anhang „nicht verarbeitet", Pipeline läuft.
  Über dem Einzel-Timeout liegt ein **Zeitbudget je Mail** (`[limits]
  pdf_time_budget_seconds`, Default 30 s): Jede Extraktion bekommt
  `min(pdf_timeout_seconds, Restbudget)`; ist das Budget aufgebraucht, gilt der Anhang wie
  beim Timeout als nicht verarbeitet und es startet gar kein Kindprozess mehr
  (ADR-029-Nachtrag, R-11). Ohne dieses Budget summierten 20 PDF-Anhänge ihre Timeouts zu
  rund 400 s Wandzeit je Mail.
- **Limits (Defaults, per Config änderbar):** Mail gesamt 25 MB (drüber ⇒
  `SanitizeError` ⇒ Metadaten-Notiz, T10), Gesamt-Klartext 30 000 Zeichen über Body und
  Anhangs-Texte hinweg (Kürzung mit `[truncated]`-Marker, `truncated=true`), PDF-Input
  10 MB, PDF-Output 50 000 Zeichen, PDF-Timeout 20 s je Anhang und 30 s Zeitbudget über
  **alle** PDF-Anhänge einer Mail, MIME-Tiefe 10 (tiefere Teile ⇒
  Metadatum „mime-tiefe ueberschritten"), höchstens 500 gelaufene MIME-Teile je Mail
  (weitere ⇒ Metadatum „mime-teile ueberschritten"), Rohwert **jedes** Headers 4096 Zeichen und
  32 Werte je Headername, Kopfzeilen des ganzen MIME-Baums 256 KiB und 4096 Zeilen (darüber
  wird der Baum abgeschnitten), höchstens 200 Empfänger in `to_addrs`,
  HTML-Konvertierung max. 1 MB und 50 000 Elemente
  **je Mail** über höchstens vier `text/html`-Teile, dazu max. 2000 Ebenen je Teil
  (drüber ⇒ Teil nicht verarbeitet, `html_rejected`), roher Klartext (Body und
  `text/plain`-Anhänge) vor der Sanitisierung auf das Sechzehnfache des Klartext-Budgets
  gedeckelt (480 000 Zeichen, als **Restbudget über die ganze Mail** — Body und alle
  Anhangstexte zusammen; setzt `truncated`, ein Anhang jenseits des Budgets gilt als nicht
  verarbeitet, sichtbar bleibt ohnehin nur `max_text_chars`), max. 2000 einzeln ausgewertete Link-Funde je Mail (`links_capped`;
  weitere Funde werden trotzdem entfernt, aber nur noch als `[Link removed]` ohne Nummer und
  ohne Fußnoteneintrag), Anhänge max. 20 Stück verarbeitet (weitere ⇒
  Metadatum), Anhang-Metadatenliste max. 100 Einträge (`blocked_attachments` zählt
  unabhängig davon korrekt).
- **Deterministische Kritiker-Fakten (F-CRIT-3):** Der Report enthält zusätzlich
  `reply_to_mismatch` (Reply-To-Adresse ≠ From-Adresse, oder ein **vorhandener, aber
  unlesbarer** Reply-To neben bekannter Absenderadresse — S-2; ein fehlender Reply-To ist
  kein Mismatch), `return_path_mismatch`
  (abweichende Domains, oder **unbekannte** From-Domain neben bekanntem Return-Path —
  zwei Unbekannte sind kein Treffer, ADR-020-Nachtrag R-9) und `auth_results` (best-effort-Parse
  von `Authentication-Results`: `spf`/`dkim`/`dmarc`, erste Nennung gewinnt).

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

**Stand der Umsetzung (WP5, Summarizer — ADR-031 bis ADR-034):** Schicht 1–4 stehen. Die
Marker tragen eine pro Aufruf aus `secrets` gezogene 96-Bit-Kennung (`llm/prompts.py`,
Zufallsquelle nur für Tests injizierbar); der System-Prompt nennt sie, und eine im Mail-Text
auftauchende Kennung wird vor dem Einbau neutralisiert. Die Custom-Instructions stehen in
einem gelabelten, auf 2000 Zeichen gedeckelten Block **vor** den als unüberschreibbar
markierten Sicherheitsregeln. Schicht 4 (`agents/summarizer.enforce_output_policy`) säubert
Markdown-Links, HTML-Tags, numerische Entities, URL-Muster inkl. Obfuskationen und
Unicode-`C*`-Zeichen aus jedem Textfeld und setzt bei jedem Fund `injection_suspected =
true`. Sie normalisiert bewusst **nicht** nach NFKC — Fullwidth-Formen, nackte IPs und
nackte Domains passieren sie und werden erst von Schicht 6 entschärft (ADR-033). Im
Kritiker-Pfad ist diese Lücke geschlossen (ADR-044).

**Stand der Umsetzung (WP6, Schicht 5 — ADR-041 bis ADR-044):** Der Kritiker
(`agents/critic.py`) hat einen eigenen System-Prompt und eigene, im Code berechnete Fakten.
Er bekommt **keine** Custom-Instructions (ADR-042) — eine Config-Vorgabe kann die
Phishing-Prüfung damit weder entschärfen noch abschalten. Mail-Inhalt und die zu prüfende
`Summary` stehen in zwei getrennten Untrusted-Blöcken mit derselben, pro Aufruf zufälligen
Kennung (ADR-041); beide Blockinhalte laufen durch die Marker-Neutralisierung, also auch
die Modellausgabe. Die deterministischen Signale (F-CRIT-3, `collect_signals`) stammen
ausschließlich aus dem `sanitization_report` und sind vom Absender nicht beeinflussbar
(T9); harte Signale heben die Risikostufe im Code an, senken sie aber nie und setzen nie
`high` (ADR-043). Schicht 4 gilt auch für das Verdict: `risk_reasons` und `notes` werden
NFKC-normalisiert und mit der Summarizer-Politik gescrubbt, ein Fund wird als eigener
Grund sichtbar gemacht (ADR-044) — das Gegenstück zum `injection_suspected`-Flag, das
`CriticVerdict` nicht hat.

**Stand der Umsetzung (WP7, Schicht 6 — ADR-035 bis ADR-040):** Verbindliche Politik des
Output-Sanitizers: Jedes Feld durchläuft Entity-Auflösung (bis Fixpunkt), NFKC +
`C*`-Entfernung, Feldkürzung, Tag-Strip, Markup-Löschung und Link-Scrub; bereits defangte
WP3-Formen werden unverändert durchgereicht. Über der **fertigen** Nachricht läuft ein
zweiter, von der Segmentierung unabhängiger Nachbrenner (`final_guard`): jedes lebende
Schema mit `://` sowie `javascript:`/`data:`-artige Schemata brechen, `www.` brechen,
Winkelklammern entfernen, `](` auftrennen, Domains und IPv4 defangen. Domains und Dateinamen
erscheinen ausschließlich mit gebrochenen Punkten, weil Messenger nackte Domains automatisch
verlinken (T7); die IDN-Punktvarianten U+3002/U+FF61 werden vorher auf `.` abgebildet.
Zustellung erfolgt ohne `parse_mode` und ohne Embeds. `DigestMessage.parts` entsteht
ausschließlich über `DigestComposer._finalize()` — es gibt keinen zweiten Weg zum Messenger.

**Nachgeschärft in WP10 (Hot-Testing, ADR-059/ADR-060; Befunde HT-1…HT-6 in
docs/TESTING.md §5):** Vier Details dieser Politik hielten nicht, was der Absatz oben
zusagt. Verbindlich ist jetzt zusätzlich:

- Der Nachbrenner läuft **nach** der Segmentierung über **jeden einzelnen Nachrichtenteil**,
  nicht nur über die ungeteilte Nachricht (ADR-059). Zugestellt wird der Teil; ein harter
  Schnitt in `split_parts` konnte vorher aus einem unauffälligen Token ein Bruchstück
  machen, das erst für sich genommen wie eine Domain aussah.
- Ein Token wird defangt, sobald **irgendeine** seiner Marken ab der zweiten TLD-förmig
  beginnt (zwei Zeichen, davon zwei Buchstaben) — nicht nur, wenn die letzte es ist. Die
  Token-Grenzen sind ASCII und lassen einen Treffer hinter `-`/`_` beginnen; `evil.comÄ`
  und `-evil.example` entgingen der Regel sonst vollständig.
- Gebrochen wird die Sequenz `://` selbst, unabhängig von Länge und Wortgrenze des
  Schema-Namens davor.
- „Bereits sichere Formen" werden **ohne** Markup-Zeichen definiert (`` ` `` `*` `|` `~` `\`
  gehören nie zu einer WP3-Form) und umfassen umgekehrt auch die gebrochenen
  Aktions-Schemata (`javascript[:]`) sowie das nackte `[.]`/`[:]`. Ersteres verhinderte
  Markup-Schmuggel, Letzteres das Wiederaufbrechen eines Defang-Tokens im zweiten
  Scrub-Durchlauf — den es real gibt (Hinweiszeilen, Sammel-Digest-Kopfzeilen nach ADR-049).

**Nachgeschärft in WP11 (Cold-Testing, Befunde CT-6/CT-7/CT-7a/CT-8/CT-11 in
docs/TESTING.md §6):**

- Die Markup-Neutralisierung ist nicht mehr nur zeichenweise: Zeilenanfangs-Markdown
  (Überschrift, Liste, Zitat, Discord-Subtext), Unterstriche am Wortrand und Massen-Pings
  (`@everyone`/`@here`) werden ebenfalls entschärft, und die Zeilen-Präfixe des
  Nachrichtenformats (`⚠️`, `📧`, `📎`, `🔍 Notes:`, `From:` — und weiterhin die deutschen
  Formen `Hinweise:`, `Von:`, `Betreff:`, `Stufe:`, `Grund:`, `PHISHING-VERDACHT:`, obwohl
  die Ausgabe englisch ist, ADR-083/CT-8) dürfen in untrusted Text nicht
  am Zeilenanfang stehen — sonst ist der einzige Warnkanal des Produkts vom Angreifer
  beschreibbar (ADR-062). Das gilt auch auf dem Fail-closed-Pfad: `compose_failure` scrubbt
  den Betreff über dieselbe Funktion (CT-7a).
- Der Injection-Verdacht (T1/F-SEC-5) entsteht zusätzlich modellunabhängig aus der
  Mail-Seite: nachgebaute Datenblock-Marker, Steuerzeichen-Ballung und wörtliche Anweisungen
  an ein Sprachmodell setzen `injection_suspected` ohne Zutun des Modells (ADR-061).
  Entfernter versteckter Text bekommt einen eigenen, wörtlich zutreffenden Hinweis.
- Mehrere unabhängige Fälschungssignale mit mindestens einem harten heben die Risikostufe
  per Code auf `high` und lösen damit das Banner aus (ADR-063, präzisiert ADR-043).

**Nachgeschärft in der Fixrunde 2026-09-11 (Befunde HC-6…HC-9, HC-22, HC-24 in
docs/TESTRUNDE-HOT-COLD.md):** Fünf Nähte derselben Art — die Regeln stimmten, ihre Ränder
nicht.

- **Der Split erzeugt keinen ungeprüften Zeilenanfang mehr (HC-6).** `neutralize_markup`
  schützt Zeilenanfänge, `split_parts` erzeugte neue: Ein harter Schnitt *innerhalb* einer
  Zeile konnte ein Struktur-Emoji oder eine Kopfzeilen-Beschriftung an den Anfang eines
  Teils schieben. Jedes Fortsetzungsstück eines solchen Schnitts trägt jetzt das neutrale
  Präfix `… `; es zählt zum Teil-Limit. `final_guard` bekommt weiterhin **keine**
  Zeilenanfangs-Regeln (sie würden die Präfixe des Composers auffressen, ADR-062).
- **Markdown überlebt auch die Link-Fußnote nicht mehr (HC-7).** Die defangte Form
  (`sanitize/links._defang`) trägt kein `` ` ``, `*`, `|`, `~`, `\` mehr; `[`/`]` bleiben,
  weil sie die Defang-Token tragen. Die Fußnote ist per SPEC-CLI §6 Teil derselben
  Nachricht, `final_guard` bleibt unverändert.
- **Kein Steuerzeichen mehr aus der Link-Erkennung (HC-8).** Der `LinkCollector` arbeitet
  intern mit `\x00`-Platzhaltern; eine seiner Regexen schnitt in ein gesetztes Token und
  ließ ein rohes U+0000 in der Zustellung zurück. Drei Schichten: Die Erkennungs-Pässe
  überspringen gesetzte Platzhalter atomar, die Zeichenklassen schließen `\x00` aus, und
  `scrub_field` entfernt `C*`-Zeichen ein **zweites Mal nach** der Link-Erkennung. Die
  Zusage aus SECURITY §4 hängt damit nicht mehr an der Korrektheit der Link-Regexe.
- **Domain- und IPv4-Erkennung kennen keine Längen- und Wortgrenzen-Schranken mehr
  (HC-9/HC-24).** Die Längendeckel in `_RE_DOMAINISH` und `links._LABEL` sind entfallen;
  die Lookarounds von `_RE_IPV4` sind ASCII-Grenzen. Die Entscheidung „ist das eine
  Domain/Adresse" fällt ausschließlich in der Formprüfung, Über-Defang ist der fail-safe
  Ausgang (ADR-036).
- **Gekürzte Dateinamen zeigen die Kürzung und die Endung (HC-22).** Kürzung in der Mitte
  mit `…`, Endung erhalten, Gesamtlänge ≤ 80 — bei einem geblockten Anhang ist die Endung
  die sicherheitsrelevante Information (ADR-040).

**Nachgeschärft in der Fixrunde 2026-09-11 (Befunde HC-5, HC-11, HC-21, HC-29 in
docs/TESTRUNDE-HOT-COLD.md):** Die modellunabhängige Erkennung aus ADR-061 hatte zwei
Lücken, und Schicht 3 gab einen Namen preis, den sie nicht hätte kennen dürfen.

- **Der Marker-Nachbau wird im Sanitizer erhoben, nicht erst in der Prompt-Schicht
  (HC-5).** Der Tag-Stripper von WP3 löscht ein `<<<MAILDIGEST-…-UNTRUSTED-…>>>`
  restlos — ausgerechnet der perfekte Nachbau verschwand also spurlos, und nur die
  verstümmelten Formen lösten Alarm aus. `sanitize/sanitizer.neutralize_forged_markers`
  läuft jetzt **vor** der Tag-Löschung, ersetzt den Fund durch das Token
  `[forged data-block marker removed]` und zählt ihn in
  `SanitizationReport.forged_markers`. Der Detektor liest zuerst dieses Feld; der
  Wortlaut-Pfad bleibt als zweite Schicht.
- **Die Phrasenliste kennt die naheliegenden Varianten (HC-21).** Possessiv (`your`,
  `deine`), `forget`/`vergiss` und der Singular fehlten — genau die Formen, die ein
  Angreifer zuerst schreibt. Die Bindung an Verb, Zeitbezug und Objekt bleibt: Ohne sie
  fiele „ignore my previous mail" unter denselben Alarm wie ein Übernahmeversuch. Im
  Werkszustand (`[llm] provider = "none"`) sind diese Indizien die **einzige** Quelle des
  Verdachts, weil es keine Modellantwort gibt, die ihn setzen könnte (ADR-076).
- **Schicht 3 nennt keinen erfundenen Feldnamen mehr (HC-11).** Bei `extra_forbidden`
  stammt der Feldpfad aus der Modellantwort und kann Mail-Inhalt tragen; er stand über
  `failure_detail` in der INFO-Logzeile. Er wird jetzt durch `<extra field>` ersetzt — in
  Protokoll und Reparatur-Prompt gleichermaßen (ADR-024).
- **Schicht 4 skaliert linear (HC-29).** Die Wortgrenzen-Suche in `_redact_tokens` war
  quadratisch: Ein whitespace-freies Modellfeld von 32 000 Zeichen brauchte 9–12 s, und
  `[llm] max_tokens` hat keine Obergrenze (T10). Die Suche läuft jetzt amortisiert linear
  (dieselben Spannen, gemessen über 4 000 Zufallseingaben); 160 000 Zeichen bleiben unter
  0,03 s.

Die Zusage aus ADR-035 („die Invariante I3 hängt nicht an der Korrektheit der
Segmentierungs-Regex") gilt damit auch für das, was der Nutzer tatsächlich sieht. Geprüft
wird sie nicht mehr nur an Beispiel-Payloads, sondern als Allaussage über zufällige
Eingaben (`tests/unit/test_hot_properties.py`, ADR-058) — deren Orakel seit HC-24
ausdrücklich **strikt großzügiger** ist als die Implementierung und seit HC-9 auch IPv4
prüft.

## 6. Betriebssicherheit

- Config `0600`; Secrets bevorzugt via Env (`MAILDIGEST_IMAP_PASSWORD`,
  `MAILDIGEST_LLM_API_KEY`, `MAILDIGEST_TELEGRAM_TOKEN`).
- **Umsetzung in der CLI (WP9, ADR-052 bis ADR-057):** `maildigest init` und jedes
  `connect-*` schreiben die Datei über `os.open(..., 0o600)` und setzen die Rechte bei
  **jedem** Schreiben neu — auch auf einer bereits vorhandenen, zu offenen Datei. Für
  IMAP-Passwort, API-Key und Bot-Token gibt es bewusst **keine** Kommandozeilen-Optionen
  (Prozessliste, Shell-History): Sie kommen aus einer Abfrage ohne Bildschirmecho
  (`getpass`, sobald ein Terminal vorhanden ist) oder aus der jeweiligen Umgebungsvariablen;
  liegt eine Variable vor, wird der Wert gar nicht erst in die Datei geschrieben (ADR-056).
  Die einzige Secret-Option ist `--webhook-url` (Discord hat keine Env-Variable im Schema).
  Fremddaten, die bei der Einrichtung anfallen — IMAP-Ordnernamen, Telegram-Chats aus
  `getUpdates`, die Antwort des Testaufrufs, der **Fehlertext des Modell-Anbieters** —
  erreichen das Terminal nur gefiltert (Zeichen-Allowlist), nur als numerische ID plus
  Chat-Typ aus fester Werteliste bzw. gar nicht (ADR-055); ein Terminal interpretiert
  sonst Steuersequenzen aus fremder Hand.
- **Terminal-Filter als letzte Schicht (HC-4, ADR-055 Nachtrag).** Die Allowlist sitzt
  nicht nur an den bekannten Einzelstellen (`cli._safe_name`, `llm/_http._foreign`),
  sondern zusätzlich an der **Ausgabestelle** in `cli.main`: Jede Fehlerzeile auf stderr
  läuft durch `foreign_text.sanitize_foreign_text`. Damit hängt der Schutz nicht daran,
  dass jeder künftige Pfad, der einen Fremdtext mitnimmt, daran gedacht hat. Zusätzlich
  maskiert `foreign_text.mask_secrets` einen von der Gegenstelle zitierten eigenen
  API-Key (voller Wert oder Präfix ab acht Zeichen) durch `***` — die Zusage aus
  SPEC-CLI §2 („Fehlermeldungen enthalten niemals … API-Keys") hängt damit nicht am
  Wohlverhalten des Anbieters.
- **Verzicht auf die Serverantwort bei `connect-mail` (I5, HC-32).** Die Meldung eines
  fehlgeschlagenen IMAP-Verbindungsversuchs nennt Host, Port, Ordner und die
  Fehlerklasse, **nie** den Antworttext des Servers: Er zitiert regelmäßig den gesendeten
  Benutzernamen und stammt aus fremder Hand. Der Diagnosewert wird stattdessen über die
  Fehlerklasse erzeugt — `ImapAuthError` (Zugangsdaten abgelehnt) zieht den
  anbieterspezifischen App-Passwort-Hinweis nach sich, jeder andere
  `ImapConnectionError` den Hinweis auf Host, Port und Netz. SPEC-CLI §4 `connect-mail`
  beschreibt dieses konservative Verhalten.
- **Atomares Schreiben der Konfiguration (ADR-081, HC-20).** `ConfigFile.save` schreibt in
  eine temporäre Datei im Zielverzeichnis (`O_WRONLY|O_CREAT|O_EXCL`, 0600), synchronisiert
  sie (`flush` + `os.fsync`) und benennt erst dann per `os.replace` um; ein Fehlschlag
  räumt die temporäre Datei auf. Ein Abbruch mitten im Schreiben (volle Platte, Quota,
  `RLIMIT_FSIZE`, EIO, Stromausfall) lässt damit die bisherige Konfiguration unverändert
  stehen — vorher blieb eine abgeschnittene Datei zurück, in der Zugangsdaten fehlten.
- IMAP nur über TLS (IMAPS 993); Zertifikatsprüfung an (kein `verify=False` irgendwo —
  Lint-Check in WP12).
- Im Mirror-Postfach wird nie gelöscht und nie expunged (ADR-064): `\Deleted` und `EXPUNGE`
  existieren in keinem Codepfad; `imap.move_processed_to` verlangt einen Server mit
  MOVE-Capability, der client-seitige Ersatz (COPY + `\Deleted` + EXPUNGE) ist bewusst nicht
  implementiert.
- Logs ohne Inhalte (NF-5): strukturierte JSON-Zeilen auf stdout, ausschließlich
  Metadaten (gekürzter Dedupe-Hash, Absender-Domain, Status, Zähler, Exception-
  **Klassenname**). Tracebacks — die Mail-Inhalte aus Fehlertexten transportieren können —
  erscheinen ausschließlich bei `log_level = "DEBUG"`; solche Logs sind entsprechend
  vertraulich zu behandeln (ADR-046/ADR-047, docs/BETRIEB.md).
- Die SQLite-DB speichert Status + Metadaten, nicht den Mail-Text (Klartext wird nur im
  Speicher gehalten). Genau **zwei** benannte Ausnahmen, beide mit bereits für den Nutzer
  freigegebenem, output-sanitisiertem Text und beide nach Zustellung geleert (ADR-048/049):
  (a) `low_digest_queue` — kritiker-geprüfte Kopfzeile, Kategorie und Absender-Domain der
  `low`-Mails; (b) `outbox` — die fertigen Nachrichtenteile einer noch nicht bestätigten
  Zustellung. Beides enthält nie Mail-Rohtext, nie Links, nie Anhänge; ohne diese
  Persistenz wären der tägliche Sammel-Digest (F-SUM-5) und die „nie stiller Verlust"-
  Zusage (F-OPS-3, ADR-008) über einen Prozessneustart hinweg nicht haltbar.
- Empfehlung in README: Mirror-Postfach bei separatem Anbieter mit eigenem, einmaligem
  Passwort; App-Passwort statt Hauptpasswort.

## 7. Invarianten-Review

**Datum:** 2026-09-11 · **Stand:** nach der Fixrunde zur Abschluss-Testrunde (HC-1 … HC-38,
docs/TESTING.md §7) · **Umfang:** `src/maildigest/`, 44 Module.
Die Momentaufnahme vom 2026-09-08 (WP12, Release 0.1.0, 41 Module, 5 `.send()`-Stellen) ist
damit überholt; die Befunde je Invariante haben sich nicht umgekehrt, sie sind an fünf Stellen
enger geworden.

**Methode.** Drei Ebenen, jede für sich unzureichend:

1. **Mechanisch.** `tests/unit/test_invarianten.py` (27 Tests) parst jedes Produktionsmodul
   mit `ast`, entfernt Docstrings und Kommentare und sucht erst dann. Das ist der
   entscheidende Unterschied zu `grep`: Die ausführlichsten Fundstellen für „expunge",
   „delete" und „parse_mode" sind die Begründungen, warum es sie **nicht** gibt — eine reine
   Textsuche bleibt daran hängen und liefert ein Ergebnis, das man nur noch glauben kann.
   Der in §6 angekündigte Lint-Check („kein `verify=False` irgendwo") ist Teil dieser Datei.
   **Neu seit der Fixrunde (HC-38):** Drei der bisher nur in Prosa geführten Zusagen dieses
   Abschnitts sind jetzt selbst mechanisch gesperrt — die Menge der `.send(`-Aufrufstellen, die
   Herkunft jedes Sende-Arguments und die Aufrufer von `compose_plain`. Beide Positivlisten
   stehen als `SEND_SITES` und `COMPOSE_PLAIN_CALLERS` in der Testdatei; eine neue Stelle fällt
   auf, statt unbemerkt zu entstehen. Die Wirksamkeit ist durch Negativproben belegt (ein
   zusätzliches `messenger.send(text)` bzw. ein Aufruf unter Umgehung des Composers lässt die
   Tests fehlschlagen).
2. **Strukturell.** Wo eine Invariante an einer Typgrenze hängt, wird die Grenze geprüft und
   nicht das Vorkommen eines Wortes (I1: welche Module dürfen `mime_bytes` überhaupt nennen;
   I2: welche Schlüssel darf ein LLM-Request-Körper enthalten).
3. **Am laufenden Programm.** Für I3/I4/I6 die vorhandenen Property- und Korpus-Tests
   (`tests/unit/test_output_sanitizer.py`, `tests/cold/test_cold_suite.py`) plus ein
   Rauchtest der installierten CLI (`maildigest test --dry-run` mit unerreichbarem Modell —
   die Metadaten-Notiz erschien, kein Inhalt).

**Was dieses Review nicht ist:** kein Beweis. Es prüft den Code gegen die Invarianten, nicht
gegen einen Angreifer. Der Blackbox-Nachweis ist die Cold-Runde (docs/TESTING.md §6); die von
§3 dort verlangte **zweite** Runde nach den `high`-Befunden CT-6/CT-9 steht aus (§7.2).

### 7.1 Befund je Invariante

| | Invariante | Befund | Beleg |
|---|---|---|---|
| **I1** | Kein LLM sieht rohe Anhänge, rohes HTML, rohe MIME-Struktur | **erfüllt** | `mime_bytes` kommt in genau drei Modulen vor: `models.py` (Felddefinition), `ingest/imap_client.py` (erzeugt `RawMail`), `sanitize/sanitizer.py` (einzige lesende Stelle). `pipeline.process_mail` gibt die `RawMail` unmittelbar nach der Sanitize-Stufe im `finally` mit `del raw` frei; alle folgenden Stufen nehmen strukturell nur `SanitizedMail` entgegen. Der HTML-Teil wird für den Divergenz-Vergleich (ADR-067) nur intern konvertiert und verlässt den Sanitizer nicht. |
| **I2** | Text-in/Text-out, keine Tools, kein Function-Calling | **erfüllt** | Die Request-Körper beider Provider sind AST-geprüft auf den geschlossenen Feldsatz `{model, max_tokens, system, messages, temperature}`. `tools`, `tool_choice`, `functions`, `function_call`, `mcp_servers`: kein einziges Vorkommen im ausführbaren Code. `LLMProvider.complete` gibt einen String zurück; es gibt keinen Rückkanal vom Modell in das Programm außer diesem String. |
| **I3** | Nachricht ohne klickbare Links, Anhänge, ausführbare Inhalte | **erfüllt** | `parse_mode` und `embeds`: kein Vorkommen. Jede zugestellte Nachricht entsteht ausschließlich über `DigestComposer._finalize()` — Feld-Scrub, `final_guard`, Split, danach `final_guard` je Teil (bis zu vier Runden, HT-4). Alle **sieben** `send()`-Aufrufstellen (`pipeline.py:_fail_closed`/`_process_sanitized`, `delivery.py:_attempt`, `runner.py:maybe_send_low_digest`/`handle_command`, `cli.py:_send_test_message`/`_announce_selftest`) speisen Objekte, die diesen Pfad gelaufen sind; `delivery.py` reicht nur bereits fertige Teile erneut ein. Liste und Argumentherkunft sind seit der Fixrunde AST-gesperrt (HC-38). Property-Tests über zufällige Modellausgaben, dazu der Angriffskorpus aus WP11 (CT-7/7a/8). **Vier Nähte sind in der Fixrunde geschlossen worden** (§5, Fixrunden-Block): Fortsetzungsstücke eines harten Zeilenschnitts tragen das neutrale Präfix `… ` und können keinen Strukturanfang mehr fälschen (HC-6); nackte IPv4 wird auch mit direkt anliegenden Nachbarzeichen gebrochen, und weder Domain- noch IPv4-Erkennung hat noch einen Längendeckel, an dem eine lange Marke vorbeikäme (HC-9, HC-24); die Link-Fußnote trägt kein Messenger-Markup mehr (HC-7); und die Zusage „kein Steuerzeichen verlässt das Modul" hängt nicht mehr an der Korrektheit der Link-Regexe, sondern an einem zweiten `C*`-Pass nach der Link-Erkennung (HC-8). Der bis dahin einzige ungescrubbte variable Anteil einer zugestellten Nachricht — der Ordnername in der `/status`-Antwort — läuft jetzt durch `scrub_plain` (HC-28); `_finalize` bleibt dabei Nachbrenner und Split, **kein** Feld-Scrub: variable Anteile scrubbt der Aufrufer. |
| **I4** | Modellausgabe ist untrusted: Schema → Kritiker → Output-Sanitizer | **erfüllt** | `Summary`/`CriticVerdict` sind pydantic-erzwungen; eine Schema-Verletzung ist `schema_invalid` und damit fail-closed. `pipeline._process_sanitized` ruft die drei Stufen in fester Reihenfolge; es gibt keinen Pfad von `summarize` direkt zum Messenger. Der Composer scrubbt jedes Modellfeld noch einmal, auch die vom Kritiker gelieferten Gründe. |
| **I5** | Secrets nie in Prompts, Logs, DB | **erfüllt, mit einer benannten Bandbreite** | Alle vier Secrets sind `pydantic.SecretStr`; `TelegramMessenger`, `DiscordMessenger` und beide LLM-Provider definieren `__repr__`/`__str__` ohne Secret. `ImapClient` hält das Passwort als einfaches Attribut, ist aber eine gewöhnliche Klasse ohne `__repr__` und ohne Dataclass-Dekorator — der Default-`repr` zeigt nur die Adresse. Sämtliche 21 `extra={…}`-Stellen wurden einzeln gelesen: gekürzte Hashes, Absender-Domain, Ordnername, Statuswerte, Zähler, Exception-**Klassennamen**. Der `JsonLogFormatter` verdichtet nicht-JSON-fähige Werte auf ihren Typnamen, statt `repr()` zu rufen. **Bandbreite:** `imap_postprocess_failed` loggt `str(exc)` statt nur den Klassennamen — dieser Text ist programm-formuliert und enthält höchstens den konfigurierten Ordnernamen und das IMAP-Statuswort (`NO`/`BAD`), keinen Mail-Inhalt und kein Secret. Bewusst so belassen: Ohne den Ordnernamen ist der häufigste Fehlerfall (falsch geschriebenes `move_processed_to`) nicht diagnostizierbar. **Vier Ergänzungen aus der Fixrunde:** (1) Das in dieser Aufzählung bisher fehlende Feld `detail` von `mail_processed`/`process_failed` stammt aus `pipeline.failure_detail(exc)` und ist bei `LLMInvalidResponse` die Fehlerliste aus `llm/schema._error_summary`. Genau ein Fehlertyp trug dort einen nicht vom Code erzeugten Namen — `extra_forbidden`, der vom Modell erfundene Schlüssel; er ist jetzt der feste Platzhalter `<extra field>`, in Logzeile **und** Reparatur-Prompt (HC-11). (2) Der Fehlertext eines Modell-Anbieters läuft vor jeder Terminalausgabe durch die Zeichen-Allowlist in `maildigest.foreign_text`, und ein darin zitierter eigener API-Key wird maskiert — die Zusage „Fehlermeldungen enthalten niemals API-Keys" hängt nicht mehr am Wohlverhalten der Gegenstelle (HC-4). (3) `connect-mail` verzichtet ausdrücklich auf die IMAP-Serverantwort und entscheidet über die Fehlerklasse (`ImapAuthError` vs. Transportfehler); der Servertext zitiert regelmäßig den gesendeten Benutzernamen (HC-32, §6). (4) Das atomare Schreiben der Konfiguration legt **keine** zweite Kopie der Secrets an: die Temp-Datei hat `0600` und verschwindet in jedem Ausgang (ADR-081). Die beiden neuen Logereignisse `mail_id_collision` und `outbox_clock_skew_corrected` tragen nur 12-stellige Hashes bzw. eine Zeilenzahl. |
| **I6** | Fail-closed | **erfüllt** | Jede Stufe in `pipeline.py` liegt in einem eigenen `try`, dessen `except Exception` in `_fail_closed` mündet; die Fehlerklassen-Abbildung ist eine geschlossene Tabelle mit `<stufe>_error` als Auffangwert. Der Ingest-Loop fängt zusätzlich pro Mail ab (`mail_processing_crashed` ⇒ `failed`), damit eine kaputte Mail den Zyklus nicht stoppt. Im Rauchtest mit unerreichbarem Modell kam die fünfzeilige Metadaten-Notiz und sonst nichts. **Zwei Präzisierungen aus der Fixrunde:** Der fail-closed-Ausgang wird nicht mehr durch einen harmlosen langen Betreff ausgelöst (HC-1) — er bleibt echten Fehlern vorbehalten. Und er ist **nicht** der Weg für zwei Lagen, in denen nichts Unsicheres passiert ist: Eine Message-ID-Kollision wird regulär unter einem abgeleiteten Schlüssel verarbeitet und nur benannt (ADR-079, HC-10); eine verschlüsselte PGP/S-MIME-Mail ebenso (ADR-082, HC-33). Eine Notiz „could not be processed safely" wäre dort sachlich falsch und nähme dem Nutzer zugleich Absender, Betreff und Anhangsliste. |
| **I7** | Anhangs-Extraktion im ressourcenbegrenzten Subprozess | **erfüllt** | `pdfminer` wird ausschließlich in `sanitize/extract_pdf.py` genannt, und dort erst **im Kindprozess** importiert — der Elternprozess lädt die Bibliothek nie. Das Kind setzt `RLIMIT_AS` vor dem Import, der Elternprozess überwacht per `subprocess.run(timeout=…)` und killt danach. Input- und Output-Grenze zusätzlich im Aufrufer. |
| **I8** | Custom-Instructions als gelabelter System-Teil, Mail strikt getrennt | **erfüllt** | `summarizer_system_prompt` baut `Rolle → Nutzer-Vorgaben → UNÜBERSCHREIBBARE SICHERHEITSREGELN`; die Reihenfolge ist als Test festgehalten. Die Mail steht in der **User**-Message zwischen Markern mit einem je Aufruf frisch gezogenen Token, dessen Nachbau im Mail-Text neutralisiert wird. `critic_system_prompt` nimmt bewusst gar keine Custom-Instructions entgegen (ADR-042) — auch das ist getestet. |

### 7.2 Was dieses Review offen lässt

1. **Zweite Cold-Runde.** docs/TESTING.md §3 verlangt sie nach Sicherheits-Findings ≥ high
   (CT-6, CT-9). Sie **steht weiterhin aus** und läuft als §6 der Fixrunde
   (docs/PLAN-FIXRUNDE.md). Die Abschluss-Testrunde vom September 2026 (docs/TESTING.md §7)
   erfüllt den Haken ausdrücklich **nicht**: Ihre Skeptikerprüfung hatte durchweg vollen
   Code-Zugriff, und die Prüfgrundlage selbst war defekt — SPEC-CLI legte den Wortlaut der
   Ausgabe deutsch fest, während die Implementierung englisch war (HC-14). Diese Voraussetzung
   ist inzwischen erfüllt: Der Vertrag beschreibt wieder, was das Programm ausgibt, und
   `tests/unit/test_hc14_spec_literals.py` hält das maschinell fest. Bis zur Runde bleibt NF-8
   `in-progress`.
2. **Kein Lauf gegen echte Gegenstellen.** Kein echtes IMAP-Postfach, keine echte LLM-API,
   kein echter Messenger — alle Nachweise stammen aus Mocks bzw. aus dem Fehlerpfad. `UID
   MOVE` ist gegen einen selbstgebauten Mock belegt, nicht gegen einen Server.
3. **Heuristik-Kalibrierung.** Phrasenliste in `detect_injection_evidence`,
   CT-15-Schwellen (≥ 5 Wörter / > 50 %) und `_HIGH_SIGNAL_COUNT = 3` sind ohne Felddaten
   gesetzt. Sie können falsch alarmieren; das ist eine Nutzbarkeits-, keine Sicherheitsfrage.

   **Miss-Richtung der Phrasenliste (HC-21).** Die Liste erkennt keine Injection, sondern
   **wörtliche** Übernahmeformeln. Paraphrasen und andere Sprachen bleiben unerkannt — das ist
   die bewusste Fehlalarm-Abwägung aus ADR-061. Neu benannt ist der Geltungsgrund: Im
   Werkszustand (`[llm] provider = "none"`) gibt es überhaupt keine Modellantwort, die
   `injection_suspected` setzen könnte; die deterministischen Indizien sind dort die **einzige**
   Quelle. Eine Lücke in ihnen ist dann kein Restrisiko, sondern ein Totalausfall der
   F-SEC-5-Anzeige. Die Liste ist in der Fixrunde um Possessiv, bestimmten Artikel, die Verben
   `forget`/`vergiss`/`missachte` und den Singular erweitert worden, die Objektbindung blieb;
   die Fehlalarmrate über den Korpus ist unverändert (4 von 49, alle vier Angriffsmails).
   Ungeeicht bleibt sie trotzdem.

   **Über-Defang und Über-Neutralisierung sind der gewählte Ausgang** (ADR-036). Das gilt seit
   der Fixrunde an zwei weiteren Stellen: `<… MAILDIGEST … UNTRUSTED …>` wird auch dann ersetzt,
   wenn ein harmloser Absender beide Wörter zufällig in spitzen Klammern schreibt (HC-5), und
   die Domain-Erkennung hat keinen Längendeckel mehr, hinter dem sich etwas verstecken ließe
   (HC-24). Preis ist jeweils ein möglicher Fehlalarm im Text, nie ein übersehener Link.
4. **`connect-mail` warnt nicht vorab**, wenn der Server kein MOVE kann oder
   `move_processed_to` nicht existiert — der Fehler fällt erst im Betrieb auf (als
   `imap_postprocess_failed`, ohne Datenverlust). In ADR-065 als sinnvolle Ergänzung
   benannt, nicht umgesetzt.
