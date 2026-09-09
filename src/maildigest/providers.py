"""Wissensbasis für die Einrichtung der drei Außenverbindungen (F-ING-3/F-LLM-2/F-MSG-2).

Reine Daten und Nachschlagefunktionen: kein Netzwerkzugriff, keine Seiteneffekte, keine
Abhängigkeit auf andere MailDigest-Module. Das Modul beantwortet die drei Fragen, an denen
die Einrichtung erfahrungsgemäß scheitert:

1. *Welchen IMAP-Host muss ich eintragen?* — :data:`PROVIDERS` kennt die großen Anbieter.
2. *Welches Passwort will der Server?* — fast alle großen Anbieter lehnen das
   Kontopasswort ab und verlangen ein eigens erzeugtes App-Passwort.
3. *Warum wird meine Anmeldung abgelehnt?* — :func:`auth_failure_hint` liefert dazu den
   anbieterspezifischen Hinweis statt einer nackten Serverfehlermeldung.

**Grundlage der Angaben.** Die Hosts und Anmeldeverfahren wurden am 2026-09-09 gegen die
echten Server geprüft (TLS-Verbindung auf Port 993, `CAPABILITY`-Abfrage). Meldet ein
Server ``LOGINDISABLED``, ist er mit MailDigest grundsätzlich **nicht** erreichbar: Das
Werkzeug kann ausschließlich Passwort-Anmeldung, kein OAuth2 (ADR-021/I2 — die
LLM-Schicht ist tool-frei, und ein OAuth-Flow gehört nicht in ein Werkzeug ohne
Browser-Kontext). Solche Anbieter sind hier bewusst mit ``supported = False`` verzeichnet,
damit die Einrichtung sofort mit einer klaren Ansage abbricht, statt den Nutzer in eine
Reihe von Anmeldefehlern laufen zu lassen.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

__all__ = [
    "DISCORD_GUIDE",
    "LLM_ANTHROPIC_GUIDE",
    "LLM_LOCAL_GUIDE",
    "MIRROR_RECOMMENDATION",
    "PROVIDERS",
    "SIGNAL_GUIDE",
    "TELEGRAM_GUIDE",
    "Provider",
    "auth_failure_hint",
    "find_by_address",
    "find_by_host",
    "host_examples",
    "setup_guide",
]


@dataclass(frozen=True)
class Provider:
    """Was MailDigest über einen E-Mail-Anbieter weiß.

    Attributes:
        key: Stabiler Bezeichner (Testreferenz, keine Anzeige).
        name: Anzeigename.
        imap_host: Der einzutragende IMAP-Host.
        imap_port: IMAPS-Port; bei allen bekannten Anbietern 993.
        hosts: Host-Schreibweisen, an denen der Anbieter erkannt wird.
        domains: Mail-Domains, an denen der Anbieter erkannt wird.
        password_kind: Was ins Passwortfeld gehört, in einem Satzteil.
        steps: Anleitung, Schritt für Schritt.
        setup_url: Direkter Einstiegspunkt beim Anbieter, falls es einen gibt.
        note: Zusätzlicher Hinweis (Stolperfallen).
        supported: ``False``, wenn Passwort-Anmeldung serverseitig abgeschaltet ist.
        unsupported_reason: Begründung, wenn ``supported`` falsch ist.
        good_for_mirror: Eignung als *neues* Spiegel-Postfach (nicht als Hauptpostfach).
    """

    key: str
    name: str
    imap_host: str
    password_kind: str
    imap_port: int = 993
    hosts: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    setup_url: str = ""
    note: str = ""
    supported: bool = True
    unsupported_reason: str = ""
    good_for_mirror: bool = False

    def match_tokens(self) -> tuple[str, ...]:
        """Alle Zeichenketten, an denen dieser Anbieter erkannt wird."""
        return (self.imap_host, *self.hosts, *self.domains)


_APP_PASSWORD = "ein App-Passwort (nicht dein Kontopasswort)"
_ACCOUNT_PASSWORD = "dein normales Kontopasswort"

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="gmail",
        name="Gmail",
        imap_host="imap.gmail.com",
        password_kind=_APP_PASSWORD,
        domains=("gmail.com", "googlemail.com"),
        steps=(
            "Bestätigung in zwei Schritten aktivieren — ohne sie bietet Google gar keine "
            "App-Passwörter an: myaccount.google.com → Sicherheit.",
            "App-Passwort erzeugen: myaccount.google.com/apppasswords (Name frei wählbar).",
            "Die 16 Zeichen ohne Leerzeichen als Passwort verwenden.",
        ),
        setup_url="https://myaccount.google.com/apppasswords",
        note=(
            "Das Google-Kontopasswort wird für IMAP seit Abschaltung der „weniger sicheren "
            "Apps\" abgelehnt — das ist der häufigste Grund für „Anmeldung fehlgeschlagen\"."
        ),
    ),
    Provider(
        key="outlook",
        name="Outlook.com / Hotmail / Live",
        imap_host="outlook.office365.com",
        password_kind="— hier gibt es keins, das funktioniert",
        hosts=("outlook.office365.com", "imap-mail.outlook.com", "outlook.com"),
        domains=("outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com", "live.de"),
        supported=False,
        unsupported_reason=(
            "Microsoft hat die Passwort-Anmeldung für IMAP abgeschaltet; der Server meldet "
            "ausdrücklich LOGINDISABLED und akzeptiert nur noch OAuth2. MailDigest kann kein "
            "OAuth2 — mit einem Outlook.com-Postfach ist keine Verbindung möglich, auch nicht "
            "mit einem App-Passwort."
        ),
        note=(
            "Ausweg: ein Spiegel-Postfach bei einem anderen Anbieter anlegen und Outlook.com "
            "dorthin weiterleiten lassen. Dein Outlook-Konto bleibt dabei unangetastet."
        ),
    ),
    Provider(
        key="gmx",
        name="GMX",
        imap_host="imap.gmx.net",
        password_kind="dein Kontopasswort — oder ein App-Passwort, falls 2FA aktiv ist",
        hosts=("imap.gmx.net", "imap.gmx.com", "imap.gmx.de"),
        domains=("gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com"),
        steps=(
            "IMAP zuerst freischalten — das ist bei GMX ab Werk AUS: im Browser einloggen → "
            "E-Mail-Einstellungen → „POP3/IMAP\" → Zugriff erlauben.",
            "Nur falls Zwei-Faktor-Anmeldung aktiv ist: Account verwalten → Login & Sicherheit "
            "→ „Anwendungsspezifische Passwörter verwalten\" → neues Passwort erzeugen.",
        ),
        note=(
            "Ohne den Schalter „POP3/IMAP-Zugriff erlauben\" scheitert die Anmeldung, obwohl "
            "Benutzername und Passwort stimmen."
        ),
        good_for_mirror=True,
    ),
    Provider(
        key="webde",
        name="WEB.DE",
        imap_host="imap.web.de",
        password_kind="dein Kontopasswort — oder ein App-Passwort, falls 2FA aktiv ist",
        domains=("web.de",),
        steps=(
            "IMAP zuerst freischalten (ab Werk AUS): im Browser einloggen → Einstellungen → "
            "„POP3/IMAP Abruf\" → Zugriff erlauben.",
            "Nur bei aktiver Zwei-Faktor-Anmeldung: unter Sicherheit ein anwendungs"
            "spezifisches Passwort erzeugen.",
        ),
        note="WEB.DE und GMX gehören zusammen; die Einrichtung ist identisch.",
        good_for_mirror=True,
    ),
    Provider(
        key="posteo",
        name="Posteo",
        imap_host="posteo.de",
        password_kind=_ACCOUNT_PASSWORD,
        hosts=("posteo.de", "imap.posteo.de"),
        domains=("posteo.de", "posteo.net", "posteo.eu", "posteo.org"),
        steps=(
            "Nichts weiter nötig: IMAP ist ab Werk offen, das Kontopasswort genügt.",
            "Nur falls du den „erweiterten Schutz\" eingeschaltet hast, brauchst du das dort "
            "vergebene App-Passwort.",
        ),
        note="Der Host heißt schlicht posteo.de — ohne imap. davor.",
        good_for_mirror=True,
    ),
    Provider(
        key="mailboxorg",
        name="mailbox.org",
        imap_host="imap.mailbox.org",
        password_kind="dein Kontopasswort — oder ein App-Passwort, falls 2FA aktiv ist",
        domains=("mailbox.org",),
        steps=(
            "Ohne Zwei-Faktor-Anmeldung genügt das Kontopasswort.",
            "Mit 2FA: Einstellungen → Sicherheit → „Anwendungsspezifische Passwörter\".",
        ),
        good_for_mirror=True,
    ),
    Provider(
        key="icloud",
        name="iCloud Mail",
        imap_host="imap.mail.me.com",
        password_kind=_APP_PASSWORD,
        domains=("icloud.com", "me.com", "mac.com"),
        steps=(
            "Zwei-Faktor-Authentifizierung muss aktiv sein — sonst erscheint die Option gar "
            "nicht.",
            "App-spezifisches Passwort erzeugen: account.apple.com → Anmeldung und Sicherheit "
            "→ „App-spezifische Passwörter\".",
        ),
        setup_url="https://account.apple.com",
        note="Das Apple-ID-Passwort selbst wird immer abgelehnt.",
    ),
    Provider(
        key="yahoo",
        name="Yahoo Mail",
        imap_host="imap.mail.yahoo.com",
        password_kind=_APP_PASSWORD,
        domains=("yahoo.com", "yahoo.de", "ymail.com"),
        steps=(
            "Kontosicherheit öffnen → „App-Passwort erzeugen\".",
            "Das erzeugte Passwort verwenden, nicht das Kontopasswort.",
        ),
    ),
    Provider(
        key="tonline",
        name="Telekom / T-Online",
        imap_host="secureimap.t-online.de",
        password_kind="dein „Passwort für E-Mail-Programme\" (nicht das Telekom-Kundenpasswort)",
        domains=("t-online.de", "magenta.de"),
        steps=(
            "Im Telekom-Kundencenter unter E-Mail-Einstellungen ein eigenes Passwort für "
            "E-Mail-Programme vergeben und den Zugriff für E-Mail-Programme freischalten.",
        ),
    ),
    Provider(
        key="ionos",
        name="IONOS / 1&1",
        imap_host="imap.ionos.de",
        password_kind=_ACCOUNT_PASSWORD,
        hosts=("imap.ionos.de", "imap.ionos.com", "imap.1und1.de"),
        domains=("ionos.de", "1und1.de"),
        steps=("Das Passwort des jeweiligen Postfachs verwenden (nicht das Vertragskonto).",),
    ),
    Provider(
        key="zoho",
        name="Zoho Mail",
        imap_host="imap.zoho.eu",
        password_kind="ein App-Passwort, sobald 2FA aktiv ist",
        hosts=("imap.zoho.eu", "imap.zoho.com"),
        domains=("zoho.com", "zohomail.eu"),
        steps=(
            "IMAP im Webmail unter Mail-Konten aktivieren.",
            "Bei aktiver 2FA ein App-Passwort erzeugen.",
        ),
        note="Konten aus der EU nutzen imap.zoho.eu, andere imap.zoho.com.",
    ),
    Provider(
        key="fastmail",
        name="Fastmail",
        imap_host="imap.fastmail.com",
        password_kind=_APP_PASSWORD,
        domains=("fastmail.com", "fastmail.fm"),
        steps=("Settings → Privacy & Security → „App Passwords\" → neues Passwort erzeugen.",),
    ),
    Provider(
        key="proton",
        name="Proton Mail",
        imap_host="127.0.0.1 (nur über die Proton-Bridge)",
        password_kind="— mit MailDigest nicht nutzbar",
        domains=("proton.me", "protonmail.com", "pm.me"),
        supported=False,
        unsupported_reason=(
            "Proton bietet kein öffentliches IMAP an. Der Zugriff läuft über die Proton-Bridge "
            "auf 127.0.0.1, die STARTTLS auf Port 1143 spricht — MailDigest verbindet "
            "ausschließlich per IMAPS und lehnt Klartext-Ports ab."
        ),
        note=(
            "Ausweg: Spiegel-Postfach bei einem anderen Anbieter anlegen und Proton dorthin "
            "weiterleiten lassen."
        ),
    ),
)

_BY_TOKEN: dict[str, Provider] = {
    token.lower(): provider for provider in PROVIDERS for token in provider.match_tokens()
}

MIRROR_RECOMMENDATION = """
Noch kein Spiegel-Postfach? Empfehlenswert sind Anbieter, bei denen IMAP ohne Umwege
funktioniert:

  Posteo (posteo.de)        ~1 €/Monat, IMAP ab Werk offen, Kontopasswort genügt.
                            Der unkomplizierteste Weg — hier gibt es keine App-Passwort-
                            und keine Freischalt-Hürde.
  mailbox.org               ~1 €/Monat, ebenso unkompliziert.
  GMX oder WEB.DE           kostenlos; IMAP muss aber erst in den Einstellungen
                            freigeschaltet werden (ein Schalter, siehe Anleitung unten).

Weniger geeignet: Outlook.com/Hotmail und Proton Mail — beide lassen die Anmeldung, die
MailDigest benutzt, grundsätzlich nicht zu (Details nennt dir `connect-mail`, sobald du
den Host einträgst).

Das Spiegel-Postfach ist ein reines Ablagefach: Es braucht keinen schönen Namen, und du
liest es nie selbst. Ein neues, leeres Konto ist besser als ein bestehendes.
""".strip()


def _normalise(value: str) -> str:
    """Vereinheitlicht Nutzereingaben für den Vergleich (Groß-/Kleinschreibung, Leerzeichen)."""
    return value.strip().strip(".").lower()


def find_by_host(host: str) -> Provider | None:
    """Sucht den Anbieter zu einem IMAP-Host.

    Erkennt neben der exakten Schreibweise auch die Registrierungs-Domain, damit
    ``imap.gmail.com``, ``gmail.com`` und ``smtp.gmail.com`` alle zu Gmail führen.
    """
    token = _normalise(host)
    if not token:
        return None
    if token in _BY_TOKEN:
        return _BY_TOKEN[token]
    labels = token.split(".")
    for start in range(len(labels) - 1):
        candidate = ".".join(labels[start:])
        if candidate in _BY_TOKEN:
            return _BY_TOKEN[candidate]
    return None


def find_by_address(address: str) -> Provider | None:
    """Sucht den Anbieter zur Mail-Adresse (alles nach dem ``@``)."""
    _, _, domain = _normalise(address).partition("@")
    return find_by_host(domain) if domain else None


def host_examples(limit: int = 6) -> str:
    """Beispielzeilen „Anbieter → Host“ für die Eingabeaufforderung."""
    shown = [p for p in PROVIDERS if p.supported][:limit]
    width = max(len(p.name) for p in shown)
    lines = [f"  {p.name.ljust(width)}  {p.imap_host}" for p in shown]
    return "\n".join(lines)


_WIDTH = 78


def _wrap(text: str, *, indent: str = "", first: str = "") -> str:
    """Bricht Fließtext auf Terminalbreite um — lange Anleitungen bleiben sonst unlesbar."""
    return textwrap.fill(
        " ".join(text.split()),
        width=_WIDTH,
        initial_indent=first or indent,
        subsequent_indent=indent,
    )


def setup_guide(provider: Provider) -> str:
    """Die vollständige Anleitung für einen erkannten Anbieter."""
    lines = [f"Erkannt: {provider.name}"]
    if not provider.supported:
        lines.append("")
        lines.append(_wrap(f"Das funktioniert mit MailDigest nicht. {provider.unsupported_reason}"))
        if provider.note:
            lines.append("")
            lines.append(_wrap(provider.note))
        return "\n".join(lines)

    lines.append(_wrap(f"Ins Passwortfeld gehört: {provider.password_kind}."))
    if provider.steps:
        lines.append("")
        for number, step in enumerate(provider.steps, start=1):
            lines.append(_wrap(step, indent="     ", first=f"  {number}. "))
    if provider.setup_url:
        lines.append("")
        lines.append(f"  Direktlink: {provider.setup_url}")
    if provider.note:
        lines.append("")
        lines.append(_wrap(provider.note, indent="  ", first="  Hinweis: "))
    return "\n".join(lines)


def auth_failure_hint(host: str) -> str:
    """Anbieterspezifischer Hinweis nach einer abgelehnten Anmeldung.

    Gibt eine leere Zeichenkette zurück, wenn der Anbieter unbekannt ist — dann bleibt es
    bei der allgemeinen Fehlermeldung, statt zu raten.
    """
    provider = find_by_host(host)
    if provider is None:
        return (
            "Häufigste Ursache: Der Anbieter verlangt für IMAP ein eigens erzeugtes "
            "App-Passwort statt des Kontopassworts. Prüfe außerdem, ob IMAP für das Konto "
            "überhaupt freigeschaltet ist."
        )
    if not provider.supported:
        return f"{provider.name}: {provider.unsupported_reason}"
    return f"{provider.name} — so bekommst du gültige Zugangsdaten:\n{setup_guide(provider)}"


# --- Sprachmodell ----------------------------------------------------------------------------
#
# Modell-IDs und Preise: Stand 2026-09-09. Sie stehen hier als Orientierung, nicht als
# Vorgabe — die Konfiguration verlangt bewusst eine ausdrückliche Modellwahl, damit kein
# Modell stillschweigend hinter dem Rücken des Nutzers Kosten verursacht (ADR-021).

LLM_ANTHROPIC_GUIDE = """
API-Key besorgen (Anthropic)
  1. console.anthropic.com öffnen und anmelden.
  2. Settings → API Keys → „Create Key", den Schlüssel (beginnt mit sk-ant-) kopieren.
  3. Unter Billing Guthaben aufladen — ohne Guthaben antwortet die API mit einem Fehler,
     obwohl der Schlüssel gültig ist. Das ist der häufigste Stolperstein.

Modell-ID (Feld `model`, exakt so eintragen)
  claude-opus-5      teuerste, stärkste Wahl        ~5 $ / 25 $ je Mio. Token
  claude-sonnet-5    guter Mittelweg bei viel Post  ~2 $ / 10 $ je Mio. Token
  claude-haiku-4-5   günstigste Wahl                ~1 $ /  5 $ je Mio. Token

Pro Mail fallen zwei Aufrufe an (Zusammenfassung + Kritiker). Für den Kritiker lässt sich
in der Konfiguration unter [llm.critic] ein eigenes, günstigeres Modell eintragen.
""".strip()

LLM_LOCAL_GUIDE = """
Lokales oder OpenAI-kompatibles Modell
  Basis-URL muss auf den API-Pfad zeigen, meist mit /v1 am Ende:
    Ollama    http://localhost:11434/v1
    LM Studio http://localhost:1234/v1
    vLLM      http://localhost:8000/v1
  Modell-ID ist der lokale Modellname (bei Ollama: `ollama list`, z. B. llama3.1).
  Einen API-Key brauchen lokale Server in der Regel nicht — Feld einfach leer lassen.

Hinweis: Der Mailinhalt verlässt bei einem lokalen Modell deinen Rechner nicht. Bei einer
Basis-URL ohne TLS im Netz ginge er dagegen im Klartext über die Leitung.
""".strip()

# --- Messenger -------------------------------------------------------------------------------

TELEGRAM_GUIDE = """
Telegram-Bot einrichten
  1. In Telegram @BotFather anschreiben und /newbot senden; Name und Benutzername
     (muss auf „bot" enden) vergeben.
  2. BotFather antwortet mit dem Token in der Form 123456789:AA... — das ist der Wert,
     der hier abgefragt wird.
  3. WICHTIG: Schreibe deinem neuen Bot jetzt selbst eine Nachricht (irgendetwas, z. B.
     „hallo"). Ohne diesen ersten Schritt kennt Telegram euren Chat nicht, und die
     Chat-ID lässt sich nicht ermitteln — ein Bot darf niemanden zuerst anschreiben.
""".strip()

DISCORD_GUIDE = """
Discord-Webhook einrichten
  1. Auf dem Zielkanal: Kanal bearbeiten → Integrationen → Webhooks → „Neuer Webhook".
  2. „Webhook-URL kopieren" — sie sieht aus wie
     https://discord.com/api/webhooks/<Zahlenfolge>/<langer Schlüssel>.
  3. Diese URL ist ein Geheimnis: Wer sie hat, kann in deinen Kanal schreiben. Sie wird
     mit Dateirechten 0600 gespeichert.
""".strip()

SIGNAL_GUIDE = """
Signal einrichten (Zusatzaufwand)
  Signal hat keine offene Bot-Schnittstelle. MailDigest spricht deshalb mit einem lokal
  laufenden signal-cli im JSON-RPC-Modus, das mit deiner Nummer registriert sein muss.
  Zugestellt wird an „Notiz an mich" — es ist bewusst der kleinste mögliche Umfang.
  Wenn du es einfach haben willst, nimm Telegram oder Discord.
""".strip()
