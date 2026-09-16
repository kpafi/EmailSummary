"""Handbuchseite (`man maildigest`) aus der argparse-Definition erzeugen (ADR-087).

Es gibt genau eine Quelle für Kommandos, Optionen und Hilfetexte: `cli.build_parser()`.
Dieses Modul rendert daraus troff (`man`-Makros). `maildigest --man` gibt das Ergebnis auf
stdout aus; die im Repo abgelegte Datei `man/maildigest.1` ist damit erzeugt, und ein Test
prüft, dass sie nicht vom Parser abweicht — sonst veraltete das Handbuch so still wie jede
von Hand gepflegte Kopie.

Die statischen Abschnitte (CONFIGURATION, ENVIRONMENT, SECURITY, …) stehen hier, weil sie
nichts sind, was argparse kennt; ihr Wortlaut ist bewusst knapp und verweist auf die
Spezifikation (docs/SPEC-CLI.md) als Vertrag.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from maildigest import __version__

__all__ = ["render_manpage"]

_HEADER = ".\\\" Generated from the argparse definition by `maildigest --man`. Do not edit by hand."

_NAME_LINE = "summarise mail from a mirror mailbox and deliver text-only digests to a messenger"

_CONFIGURATION = """\
The configuration is a TOML file with file mode 0600. Without \\fB\\-\\-config\\fR the
environment variable \\fBMAILDIGEST_CONFIG\\fR is used, otherwise \\fIconfig.toml\\fR in the
current directory. \\fBmaildigest init\\fR writes it with the complete field set; the
\\fBconnect\\-*\\fR commands fill in the sections they own. The full field reference is
docs/SPEC\\-CLI.md section 5. The sections in short:
.TP
.B [general]
language and length of the summaries, importance threshold, time of the daily digest,
state database, log level.
.TP
.B [imap]
host, port (IMAPS only), username, password (better: environment variable), folder, poll
interval, optional target folder for processed mail.
.TP
.B [llm], [llm.critic]
provider (none, anthropic, openai_compatible), model ID, API key, base URL, optional
response token limit (absent = no limit); [llm.critic] overrides fields for the critic.
.TP
.B [summarizer]
custom instructions, editable with \\fBmaildigest instructions\\fR.
.TP
.B [links]
whether a defanged link list is appended as a footnote.
.TP
.B [messenger], [messenger.telegram], [messenger.discord], [messenger.signal]
active channel and its credentials; Telegram may accept /digest and /status from the chat.
.TP
.B [limits]
size, count and time limits of the sanitizer (mail size, text length, attachments, PDF
extraction, HTML conversion)."""

_EXIT_STATUS = """\
.TP
.B 0
Success.
.TP
.B 1
Error: connection, login, invalid configuration, failed test call, delivery failure, or a
self\\-test that ended fail\\-closed.
.TP
.B 2
Usage error: unknown command or option, value out of range, missing required input in
non\\-interactive mode, end of input while a question was pending."""

_ENVIRONMENT = """\
.TP
.B MAILDIGEST_CONFIG
Path of the configuration file when \\fB\\-\\-config\\fR is not given.
.TP
.B MAILDIGEST_IMAP_PASSWORD
IMAP password; takes precedence over the file and keeps the secret out of it.
.TP
.B MAILDIGEST_LLM_API_KEY
API key of the language model provider; same rule.
.TP
.B MAILDIGEST_TELEGRAM_TOKEN
Telegram bot token; same rule.
.TP
.B VISUAL, EDITOR
Editor used by \\fBmaildigest instructions \\-\\-edit\\fR (fallback: nano, then vi)."""

_FILES = """\
.TP
.I config.toml
Configuration, file mode 0600. Location: see CONFIGURATION.
.TP
.I state.db
SQLite state next to the configuration (path configurable in [general]): which mail was
seen, delivery queue, daily\\-digest queue. Contains hashes and status values, never mail
content or secrets. Deleting it makes every unread mail in the mirror mailbox new again.
.TP
.I man/maildigest.1
This manual page as shipped in the repository; regenerate with
\\fBmaildigest \\-\\-man > man/maildigest.1\\fR."""

_SECURITY = """\
MailDigest never forwards a link, an attachment or HTML to the messenger: URLs appear as
\\fB[Link #n: domain]\\fR with the dots broken, attachments as name and size only. The two
model stages see plain text prepared in code and can do nothing but answer with text: no
actions, no network or file access. Their output is treated as untrusted: schema check,
critic, output sanitizer. Anything
that fails is delivered as a metadata notice instead of content. Secrets never reach a
prompt, a log or the state database. The full threat model and the invariants are in
docs/SECURITY.md."""

_EXAMPLES = """\
.nf
maildigest init
maildigest connect\\-mail
maildigest connect\\-messenger
maildigest test \\-\\-dry\\-run
maildigest run
.fi
.PP
Cron instead of continuous operation:
.PP
.nf
*/5 * * * *  maildigest \\-\\-config /etc/maildigest.toml \\-\\-non\\-interactive run \\-\\-once
.fi
.PP
Tell the summariser what matters to you:
.PP
.nf
maildigest instructions \\-\\-add "Invoices and appointments are always important."
.fi
.PP
Read this manual without installing it:
.PP
.nf
maildigest \\-\\-man | man \\-l \\-
.fi"""


def _escape(text: str) -> str:
    """Macht Fließtext troff-sicher: Backslash, Bindestrich, Zeilenanfänge."""
    out = text.replace("\\", "\\e").replace("-", "\\-")
    lines = []
    for line in out.split("\n"):
        if line.startswith((".", "\'")):
            line = "\\&" + line
        lines.append(line)
    return "\n".join(lines)


def _paragraphs(text: str) -> str:
    """Absätze aus argparse-Texten: Leerzeile trennt, eingerückte Blöcke bleiben wörtlich."""
    blocks: list[str] = []
    for raw in text.strip().split("\n\n"):
        lines = raw.split("\n")
        if all(line.startswith("  ") or not line.strip() for line in lines):
            body = "\n".join(_escape(line[2:]) for line in lines)
            blocks.append(f".nf\n{body}\n.fi")
        else:
            blocks.append(_escape(" ".join(line.strip() for line in lines)))
    return "\n.PP\n".join(blocks)


def _option_label(action: argparse.Action) -> str:
    """`\\fB\\-\\-option\\fR \\fIWERT\\fR` bzw. die Auswahlliste."""
    names = ", ".join(f"\\fB{_escape(name)}\\fR" for name in action.option_strings)
    if action.nargs == 0:
        return names
    if action.choices is not None:
        value = "{" + ",".join(str(choice) for choice in action.choices) + "}"
    else:
        value = str(action.metavar or action.dest.upper())
    return f"{names} \\fI{_escape(value)}\\fR"


def _options(actions: Iterable[argparse.Action], *, skip: set[str]) -> str:
    entries = []
    for action in actions:
        if not action.option_strings or set(action.option_strings) & skip:
            continue
        if action.help is argparse.SUPPRESS:
            continue
        entries.append(f".TP\n{_option_label(action)}\n{_escape(action.help or '')}")
    return "\n".join(entries)


def _subcommands(parser: argparse.ArgumentParser) -> list[tuple[str, str, argparse.ArgumentParser]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            summaries = {pseudo.dest: pseudo.help or "" for pseudo in action._choices_actions}
            return [(name, summaries.get(name, ""), sub) for name, sub in action.choices.items()]
    return []


def render_manpage(parser: argparse.ArgumentParser, *, version: str = __version__) -> str:
    """Rendert die Handbuchseite als troff-Text (Abschnitt 1, Nutzerkommandos)."""
    global_actions = [a for a in parser._actions if a.option_strings]
    global_names = {name for a in global_actions for name in a.option_strings}
    subcommands = _subcommands(parser)

    out: list[str] = [
        _HEADER,
        f'.TH MAILDIGEST 1 "MailDigest {version}" "MailDigest {version}" "User Commands"',
        ".SH NAME",
        f"maildigest \\- {_escape(_NAME_LINE)}",
        ".SH SYNOPSIS",
        ".B maildigest",
        "[\\fB\\-\\-config\\fR \\fIPATH\\fR] [\\fB\\-\\-non\\-interactive\\fR] "
        "\\fICOMMAND\\fR [\\fIOPTIONS\\fR]",
        ".br",
        ".B maildigest",
        "\\fB\\-\\-help\\fR | \\fB\\-\\-man\\fR",
        ".SH DESCRIPTION",
        _paragraphs(parser.description or ""),
        ".PP",
        "Commands: " + ", ".join(f"\\fB{_escape(name)}\\fR" for name, _s, _p in subcommands)
        + ". Global options may be given before or after the command name.",
        ".SH COMMANDS",
    ]
    for name, summary, sub in subcommands:
        out.append(f'.SS "maildigest {_escape(name)}"')
        out.append(_escape(summary[:1].upper() + summary[1:]) + ".")
        if sub.description:
            out.append(".PP")
            out.append(_paragraphs(sub.description))
        options = _options(sub._actions, skip=global_names)
        if options:
            out.append(".PP")
            out.append("Options:")
            out.append(options)
        if sub.epilog:
            out.append(".PP")
            out.append(_paragraphs(sub.epilog))
    out += [
        ".SH GLOBAL OPTIONS",
        _options(global_actions, skip=set()),
        ".SH CONFIGURATION",
        _CONFIGURATION,
        ".SH EXIT STATUS",
        _EXIT_STATUS,
        ".SH ENVIRONMENT",
        _ENVIRONMENT,
        ".SH FILES",
        _FILES,
        ".SH EXAMPLES",
        _EXAMPLES,
        ".SH SECURITY",
        _SECURITY,
        ".SH SEE ALSO",
        "docs/SPEC\\-CLI.md (the contract for every command, question and message line),",
        "docs/SECURITY.md, docs/OPERATIONS.md (systemd and cron), README.md.",
    ]
    return "\n".join(out) + "\n"
