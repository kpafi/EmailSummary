# Fedora-Paket für MailDigest. Die Versionsnummer wird beim Bauen des SRPM aus
# pyproject.toml eingesetzt (.copr/Makefile) — sie steht nirgends doppelt.
Name:           maildigest
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Summarises mail from a mirror mailbox into text-only digests

License:        MIT
URL:            https://github.com/kpafi/EmailSummary
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel

%description
MailDigest reads a mirror mailbox over IMAP, strips every message down to plain
text, filters it by importance, checks it for phishing traits and delivers a
text-only digest to a messenger such as Telegram or Discord.

Nothing that arrives by mail is ever rendered, executed or followed: HTML is
converted to text, attachments are read only as text, links are shown as bare
strings and the digest itself is plain text. A language model is optional --
without one MailDigest still delivers a labelled extract and every
deterministic warning.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l maildigest
# Die Handbuchseite liegt als shared-data schon im Wheel; der Aufruf stellt sie
# unabhängig davon sicher, damit `man maildigest` nach der Installation trägt.
install -Dpm 0644 man/maildigest.1 %{buildroot}%{_mandir}/man1/maildigest.1

%check
%pyproject_check_import

%files -f %{pyproject_files}
%doc README.md CHANGELOG.md
%{_bindir}/maildigest
%{_mandir}/man1/maildigest.1*

%changelog
* Wed Sep 16 2026 Samuel Krapf <samukrapf@gmail.com> - @VERSION@-1
- Automatisch aus dem Git-Tag gebaut; Änderungen siehe CHANGELOG.md.
