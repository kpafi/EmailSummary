# Fedora package for MailDigest. The version number is substituted from
# pyproject.toml while the SRPM is built (.copr/Makefile) — it is never written
# down twice.
Name:           maildigest
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Summarises mail from a mirror mailbox into text-only digests

License:        MIT
URL:            https://github.com/kpafi/maildigest
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel

# Weak dependencies, only for `maildigest selfhost-mail` (F-ING-4, ADR-089): that command
# generates a Postfix/Dovecot configuration for a self-hosted mirror mailbox and checks
# the result over the network. MailDigest itself needs none of them, so they are Suggests
# and not Requires; python3-dns carries the optional MX check, without which that one
# step reports "skipped".
Suggests:       postfix
Suggests:       dovecot
Suggests:       certbot
Suggests:       python3-dns

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
# The manual page already ships in the wheel as shared-data; this call makes
# sure of it independently, so that `man maildigest` works after installation.
install -Dpm 0644 man/maildigest.1 %{buildroot}%{_mandir}/man1/maildigest.1

%check
%pyproject_check_import

%files -f %{pyproject_files}
%doc README.md CHANGELOG.md
%{_bindir}/maildigest
%{_mandir}/man1/maildigest.1*

%changelog
* Wed Sep 16 2026 Samuel Krapf <samukrapf@gmail.com> - @VERSION@-1
- Built automatically from the git tag; see CHANGELOG.md for the changes.
