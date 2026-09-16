# COPR ruft beim SCM-Paket mit Baumethode „make_srpm" genau dieses Ziel auf:
#
#     make -f .copr/Makefile srpm outdir=<verzeichnis>
#
# Es baut das Quellpaket aus dem ausgecheckten Stand — die Version kommt aus
# pyproject.toml, damit es keine zweite Stelle mit einer Versionsnummer gibt.
srpm:
	dnf -y install rpm-build tar sed
	set -eu; \
	version=$$(sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml | head -1); \
	test -n "$$version"; \
	echo "==> MailDigest $$version"; \
	rm -rf .srpm-build; \
	mkdir -p .srpm-build/SOURCES .srpm-build/SPECS "maildigest-$$version"; \
	git archive --format=tar --prefix="maildigest-$$version/" HEAD \
	  | gzip -9 > ".srpm-build/SOURCES/maildigest-$$version.tar.gz"; \
	sed "s/@VERSION@/$$version/g" packaging/rpm/maildigest.spec \
	  > .srpm-build/SPECS/maildigest.spec; \
	rpmbuild -bs .srpm-build/SPECS/maildigest.spec \
	  --define "_topdir $$(pwd)/.srpm-build" \
	  --define "_sourcedir $$(pwd)/.srpm-build/SOURCES" \
	  --define "_srcrpmdir $(outdir)"
