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
    "LLM_PRESETS",
    "MIRROR_RECOMMENDATION",
    "PROVIDERS",
    "SIGNAL_GUIDE",
    "TELEGRAM_GUIDE",
    "LlmPreset",
    "Provider",
    "auth_failure_hint",
    "find_by_address",
    "find_by_host",
    "find_preset",
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


_APP_PASSWORD = "an app password (not your account password)"
_ACCOUNT_PASSWORD = "your normal account password"

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        key="gmail",
        name="Gmail",
        imap_host="imap.gmail.com",
        password_kind=_APP_PASSWORD,
        domains=("gmail.com", "googlemail.com"),
        steps=(
            "Turn on 2-Step Verification first — without it Google does not offer app "
            "passwords at all: myaccount.google.com -> Security.",
            "Create an app password: myaccount.google.com/apppasswords (name it anything).",
            "Use those 16 characters, without the spaces, as the password.",
        ),
        setup_url="https://myaccount.google.com/apppasswords",
        note=(
            "Google stopped accepting the account password for IMAP when it removed "
            '"less secure apps" — this is by far the most common cause of '
            '"authentication failed" when host and username look correct.'
        ),
    ),
    Provider(
        key="outlook",
        name="Outlook.com / Hotmail / Live",
        imap_host="outlook.office365.com",
        password_kind="- there is none that works",
        hosts=("outlook.office365.com", "imap-mail.outlook.com", "outlook.com"),
        domains=("outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com", "live.de"),
        supported=False,
        unsupported_reason=(
            "Microsoft turned off password authentication for IMAP; the server explicitly "
            "reports LOGINDISABLED and accepts OAuth2 only. MailDigest cannot do OAuth2, so "
            "an Outlook.com mailbox cannot be connected at all — not even with an app password."
        ),
        note=(
            "Way around it: create the mirror mailbox at a different provider and have "
            "Outlook.com forward to it. Your Outlook account stays untouched."
        ),
    ),
    Provider(
        key="gmx",
        name="GMX",
        imap_host="imap.gmx.net",
        password_kind="your account password — or an app password if you use 2FA",
        hosts=("imap.gmx.net", "imap.gmx.com", "imap.gmx.de"),
        domains=("gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com"),
        steps=(
            "Switch IMAP on first — it is OFF by default at GMX: log in via the browser, "
            'then Email settings -> "POP3/IMAP" -> allow access.',
            "Only if two-factor login is active: Manage account -> Login & Security -> "
            '"Manage application-specific passwords" -> create a new one.',
        ),
        note=(
            'Without the "allow POP3/IMAP access" switch the login fails even though '
            "username and password are correct."
        ),
        good_for_mirror=True,
    ),
    Provider(
        key="webde",
        name="WEB.DE",
        imap_host="imap.web.de",
        password_kind="your account password — or an app password if you use 2FA",
        domains=("web.de",),
        steps=(
            "Switch IMAP on first (OFF by default): log in via the browser, then Settings "
            '-> "POP3/IMAP retrieval" -> allow access.',
            "Only with two-factor login active: create an application-specific password "
            "under Security.",
        ),
        note="WEB.DE and GMX belong to the same company; setup is identical.",
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
            "Nothing else to do: IMAP is open by default and the account password works.",
            'Only if you enabled the "extended protection" option do you need the app '
            "password set there.",
        ),
        note="The host is simply posteo.de — no imap. in front of it.",
        good_for_mirror=True,
    ),
    Provider(
        key="mailboxorg",
        name="mailbox.org",
        imap_host="imap.mailbox.org",
        password_kind="your account password — or an app password if you use 2FA",
        domains=("mailbox.org",),
        steps=(
            "Without two-factor login the account password is enough.",
            'With 2FA: Settings -> Security -> "Application-specific passwords".',
        ),
        good_for_mirror=True,
    ),
    Provider(
        key="infomaniak",
        name="Infomaniak (ik.me)",
        imap_host="mail.infomaniak.com",
        password_kind=_ACCOUNT_PASSWORD,
        domains=("ik.me", "ikmail.com", "etik.com", "infomaniak.com"),
        steps=(
            "Create a free account at infomaniak.com/en/free-email "
            "(@ik.me, 20 GB, ad-free, servers in Switzerland).",
            "The account password works; with two-factor login enabled, create an "
            "application password in the account area.",
        ),
        note="The host is mail.infomaniak.com for every address variant.",
        good_for_mirror=True,
    ),
    Provider(
        key="icloud",
        name="iCloud Mail",
        imap_host="imap.mail.me.com",
        password_kind=_APP_PASSWORD,
        domains=("icloud.com", "me.com", "mac.com"),
        steps=(
            "Two-factor authentication must be on — otherwise the option does not appear.",
            "Create an app-specific password: account.apple.com -> Sign-In and Security -> "
            '"App-Specific Passwords".',
        ),
        setup_url="https://account.apple.com",
        note="The Apple ID password itself is always rejected.",
    ),
    Provider(
        key="yahoo",
        name="Yahoo Mail",
        imap_host="imap.mail.yahoo.com",
        password_kind=_APP_PASSWORD,
        domains=("yahoo.com", "yahoo.de", "ymail.com"),
        steps=(
            'Open Account Security -> "Generate app password".',
            "Use the generated password, not the account password.",
        ),
    ),
    Provider(
        key="tonline",
        name="Telekom / T-Online",
        imap_host="secureimap.t-online.de",
        password_kind='your "password for email programs" (not the Telekom customer password)',
        domains=("t-online.de", "magenta.de"),
        steps=(
            "In the Telekom customer centre, set a separate password for email programs "
            "under email settings and allow access for email programs.",
        ),
    ),
    Provider(
        key="ionos",
        name="IONOS / 1&1",
        imap_host="imap.ionos.de",
        password_kind=_ACCOUNT_PASSWORD,
        hosts=("imap.ionos.de", "imap.ionos.com", "imap.1und1.de"),
        domains=("ionos.de", "1und1.de"),
        steps=("Use the password of the mailbox itself, not of the contract account.",),
    ),
    Provider(
        key="zoho",
        name="Zoho Mail",
        imap_host="imap.zoho.eu",
        password_kind="an app password once 2FA is active",
        hosts=("imap.zoho.eu", "imap.zoho.com"),
        domains=("zoho.com", "zohomail.eu"),
        steps=(
            "Enable IMAP in webmail under Mail Accounts (paid plans only).",
            "With 2FA active, create an app password.",
        ),
        note=(
            "Careful: the free Zoho plan no longer includes IMAP — new accounts need a paid "
            "plan for it. Accounts in the EU use imap.zoho.eu, others imap.zoho.com."
        ),
    ),
    Provider(
        key="fastmail",
        name="Fastmail",
        imap_host="imap.fastmail.com",
        password_kind=_APP_PASSWORD,
        domains=("fastmail.com", "fastmail.fm"),
        steps=('Settings -> Privacy & Security -> "App Passwords" -> create a new one.',),
    ),
    Provider(
        key="proton",
        name="Proton Mail",
        imap_host="127.0.0.1 (via Proton Bridge only)",
        password_kind="- not usable with MailDigest",
        domains=("proton.me", "protonmail.com", "pm.me"),
        supported=False,
        unsupported_reason=(
            "Proton offers no public IMAP. Access goes through Proton Bridge on 127.0.0.1, "
            "which speaks STARTTLS on port 1143 — MailDigest connects over IMAPS only and "
            "rejects plaintext ports."
        ),
        note=(
            "Way around it: create the mirror mailbox at a different provider and have "
            "Proton forward to it."
        ),
    ),
)


_BY_TOKEN: dict[str, Provider] = {
    token.lower(): provider for provider in PROVIDERS for token in provider.match_tokens()
}

MIRROR_RECOMMENDATION = """
No mirror mailbox yet? These providers let IMAP work without detours:

  Free of charge
    GMX or WEB.DE           IMAP has to be switched on once in the settings
                            (one toggle; instructions follow below).
    Infomaniak (@ik.me)     20 GB, ad-free, Switzerland; account password is enough.
    Gmail                   works, but requires two-factor login and a separately
                            created app password.

  A euro a month, but no hurdles at all
    Posteo (posteo.de)      IMAP open by default, account password is enough.
    mailbox.org             equally straightforward.

Not usable: Outlook.com/Hotmail and Proton Mail refuse the kind of login MailDigest uses,
and the free Zoho plan no longer includes IMAP. (connect-mail tells you the details as
soon as you enter the host.)

The mirror mailbox is a plain drop box: it needs no pretty name, and you never read it
yourself. A fresh, empty account is better than an existing one.
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
    lines = [f"Detected: {provider.name}"]
    if not provider.supported:
        lines.append("")
        lines.append(_wrap(f"This does not work with MailDigest. {provider.unsupported_reason}"))
        if provider.note:
            lines.append("")
            lines.append(_wrap(provider.note))
        return "\n".join(lines)

    lines.append(_wrap(f"The password field needs: {provider.password_kind}."))
    if provider.steps:
        lines.append("")
        for number, step in enumerate(provider.steps, start=1):
            lines.append(_wrap(step, indent="     ", first=f"  {number}. "))
    if provider.setup_url:
        lines.append("")
        lines.append(f"  Direct link: {provider.setup_url}")
    if provider.note:
        lines.append("")
        lines.append(_wrap(provider.note, indent="  ", first="  Note: "))
    return "\n".join(lines)


def auth_failure_hint(host: str) -> str:
    """Anbieterspezifischer Hinweis nach einer abgelehnten Anmeldung.

    Gibt einen allgemeinen Hinweis zurück, wenn der Anbieter unbekannt ist — geraten wird
    nicht, aber die mit Abstand häufigste Ursache wird trotzdem genannt.
    """
    provider = find_by_host(host)
    if provider is None:
        return (
            "Most common cause: the provider requires a separately created app password "
            "for IMAP instead of the account password. Also check whether IMAP is enabled "
            "for the account at all."
        )
    if not provider.supported:
        return f"{provider.name}: {provider.unsupported_reason}"
    return f"{provider.name} — how to get working credentials:\n{setup_guide(provider)}"


# --- Sprachmodell ----------------------------------------------------------------------------
#
# Modell-IDs und Preise: Stand 2026-09-09. Sie stehen hier als Orientierung, nicht als
# Vorgabe — die Konfiguration verlangt bewusst eine ausdrückliche Modellwahl, damit kein
# Modell stillschweigend hinter dem Rücken des Nutzers Kosten verursacht (ADR-021).

LLM_ANTHROPIC_GUIDE = """
Getting an API key (Anthropic)
  1. Open console.anthropic.com and sign in.
  2. Settings -> API Keys -> "Create Key", copy the key (it starts with sk-ant-).
  3. Add credit under Billing — without credit the API returns an error even though the
     key is valid. That is the most common stumbling block here.

Model ID (field `model`, enter it exactly)
  claude-opus-5      most capable, most expensive   ~$5 / $25 per million tokens
  claude-sonnet-5    good middle ground for volume  ~$2 / $10 per million tokens
  claude-haiku-4-5   cheapest                       ~$1 /  $5 per million tokens

Every mail costs two calls (summary + critic). You can give the critic its own, cheaper
model under [llm.critic] in the configuration.
""".strip()

LLM_LOCAL_GUIDE = """
Local or OpenAI-compatible model
  The base URL must point at the API path, usually ending in /v1:
    Ollama     http://localhost:11434/v1
    LM Studio  http://localhost:1234/v1
    vLLM       http://localhost:8000/v1
  The model ID is the local model name (with Ollama: `ollama list`, e.g. llama3.1).
  Local servers usually need no API key at all — just leave the field empty.

Note: with a local model the mail content never leaves your machine. With a base URL over
plain HTTP on a network, it would travel in the clear.
""".strip()

# --- Messenger -------------------------------------------------------------------------------

TELEGRAM_GUIDE = """
Setting up a Telegram bot
  1. In Telegram, message @BotFather and send /newbot; pick a name and a username
     (it has to end in "bot").
  2. BotFather replies with the token, in the form 123456789:AA... — that is the value
     asked for here.
  3. IMPORTANT: now send your new bot a message yourself (anything, e.g. "hello").
     Without this first step Telegram does not know your chat, and the chat ID cannot be
     discovered — a bot is never allowed to message someone first.
""".strip()

DISCORD_GUIDE = """
Setting up a Discord webhook
  1. On the target channel: Edit Channel -> Integrations -> Webhooks -> "New Webhook".
  2. Click "Copy Webhook URL" — it looks like
     https://discord.com/api/webhooks/<digits>/<long key>.
  3. That URL is a secret: anyone holding it can post to your channel. It is stored with
     file permissions 0600.
""".strip()

SIGNAL_GUIDE = """
Setting up Signal (extra effort)
  Signal has no open bot interface. MailDigest therefore talks to a locally running
  signal-cli in JSON-RPC mode, registered with your phone number. Messages are delivered
  to "Note to Self" — deliberately the smallest possible scope.
  If you want it simple, use Telegram or Discord instead.
""".strip()


# --- Auswahl für `connect-llm` ---------------------------------------------------------------
#
# Endpunkte am 2026-09-09 geprüft (POST ohne Schlüssel -> 401/403, also erreichbar und
# OpenAI-förmig). Modell-IDs stehen bewusst NICHT drin: Sie wechseln bei den Gratis-Anbietern
# im Monatsrhythmus, ein hartkodierter Name wäre schneller falsch als die Dokumentation.


@dataclass(frozen=True)
class LlmPreset:
    """Eine wählbare Option in `maildigest connect-llm`.

    Attributes:
        key: Stabiler Bezeichner (Testreferenz).
        label: Zeile in der Auswahlliste.
        provider: Wert für `[llm] provider`.
        base_url: Vorbelegung für `[llm] base_url` (leer = Anbieter-Default).
        needs_key: Ob ein API-Schlüssel nötig ist.
        needs_model: Ob ein Modellname eingetragen werden muss.
        detail: Erklärung, die vor der Abfrage gedruckt wird.
    """

    key: str
    label: str
    provider: str
    base_url: str = ""
    needs_key: bool = True
    needs_model: bool = True
    detail: str = ""


LLM_PRESETS: tuple[LlmPreset, ...] = (
    LlmPreset(
        key="none",
        label="No language model — works immediately, nothing to sign up for (default)",
        provider="none",
        needs_key=False,
        needs_model=False,
        detail=(
            "MailDigest then delivers no summary, but an honestly labelled excerpt of the "
            "mail plus everything the program determines on its own: sender, defanged "
            "links, blocked attachments, failed SPF/DKIM checks, punycode domains and "
            "hidden-text findings. The phishing warnings keep working — they are computed "
            "in code, never by a model. Good enough to answer \"did anything important "
            "arrive?\"; connect a model later for real summaries."
        ),
    ),
    LlmPreset(
        key="groq",
        label="Groq — free tier, no credit card (fast, OpenAI-compatible)",
        provider="openai_compatible",
        base_url="https://api.groq.com/openai/v1",
        detail=(
            "Sign up at console.groq.com and create an API key; no card required. The free "
            "tier is rate-limited per minute and per day, which is ample for a private "
            "mailbox. Pick a model from the list in the Groq console and enter its exact ID."
        ),
    ),
    LlmPreset(
        key="openrouter",
        label="OpenRouter — free models, no credit card",
        provider="openai_compatible",
        base_url="https://openrouter.ai/api/v1",
        detail=(
            "Sign up at openrouter.ai and create a key. Models whose ID ends in \":free\" "
            "cost nothing — openrouter.ai/models lists which ones currently do. Enter the "
            "full ID including the \":free\" suffix.\n"
            "\n"
            "Two things to know before you pick this:\n"
            "  * Budget: MailDigest makes TWO calls per mail (summary + critic). The free "
            "quota is 50 requests per day until you have ever bought 10 dollars of credit, "
            "which works out at roughly 25 mails a day. Above that it becomes 1000 per day. "
            "A busy mailbox will hit the lower cap.\n"
            "  * Privacy: free endpoints may be served under terms that allow the content "
            "to be used for model improvement — and mail content is exactly what you would "
            "be sending. OpenRouter has an account setting to refuse providers that train "
            "on data; turn it on before you point this at a real mailbox. If that matters "
            "to you, the local model option keeps everything on your machine."
        ),
    ),
    LlmPreset(
        key="cerebras",
        label="Cerebras — free tier, no credit card",
        provider="openai_compatible",
        base_url="https://api.cerebras.ai/v1",
        detail=(
            "Sign up at cloud.cerebras.ai and create a key. Generous daily token budget; "
            "model IDs are listed in the console."
        ),
    ),
    LlmPreset(
        key="ollama",
        label="Local model (Ollama, LM Studio, vLLM) — free and fully private",
        provider="openai_compatible",
        base_url="http://localhost:11434/v1",
        needs_key=False,
        detail=(
            "Nothing leaves your machine — the strongest option for mail content. Requires "
            "installing Ollama (ollama.com) and pulling a model once, which needs a few "
            "gigabytes of disk and RAM. Then `ollama list` shows the model name to enter "
            "here. No API key needed. Adjust the base URL for LM Studio (port 1234) or "
            "vLLM (port 8000)."
        ),
    ),
    LlmPreset(
        key="anthropic",
        label="Anthropic — paid, best summary quality",
        provider="anthropic",
        detail=LLM_ANTHROPIC_GUIDE,
    ),
    LlmPreset(
        key="openai_compatible",
        label="Other OpenAI-compatible endpoint — enter the base URL yourself",
        provider="openai_compatible",
        detail=LLM_LOCAL_GUIDE,
    ),
)


def find_preset(wanted: str, *, base_url: str = "") -> LlmPreset:
    """Wählt die Vorlage zu einem `--provider`- bzw. `[llm] provider`-Wert (HC-3).

    Das Feld `provider` ist **mehrdeutig**: Fünf der sieben Vorlagen tragen
    `openai_compatible`. Eine Suche darüber lieferte die erste passende Vorlage (Groq) und
    damit fremden Erklärtext samt fremder `base_url`. Deshalb entscheidet zuerst der
    bisherige Dateiwert `base_url` (sofern einer vorliegt), dann der stabile `key`; erst
    danach wird über `provider` gesucht, und eine mehrdeutige Suche endet bei der
    generischen Vorlage (kein Anbietertext, keine URL-Vorbelegung).

    Args:
        wanted: Wert aus `--provider` oder aus der Konfigurationsdatei.
        base_url: Bisheriger Dateiwert; entscheidet bei Mehrdeutigkeit die Vorauswahl der
            interaktiven Liste (wer Groq eingetragen hat, bekommt Groq vorgeschlagen).

    Returns:
        Die passende Vorlage; bei unbekanntem Wert die erste (»kein Sprachmodell«).
    """
    matches = [preset for preset in LLM_PRESETS if preset.provider == wanted]
    if base_url:
        # Der Dateiwert benennt die Vorlage genauer als das mehrdeutige `provider`: Wer
        # den Groq-Endpunkt eingetragen hat, meint Groq.
        for preset in matches:
            if preset.base_url == base_url:
                return preset
    for preset in LLM_PRESETS:
        if preset.key == wanted:
            return preset
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return LLM_PRESETS[0]
    generic = next((preset for preset in LLM_PRESETS if preset.key == "openai_compatible"), None)
    return generic if generic is not None else LLM_PRESETS[0]


LLM_CHOICE_INTRO = """
How should mail be summarised? Every option except the first needs an account with the
respective provider — MailDigest deliberately ships no key of its own: this program is
open source, so an embedded key would be scraped and revoked within days.
""".strip()
