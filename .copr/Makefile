# For an SCM package with build method "make_srpm", COPR calls exactly this
# target:
#
#     make -f .copr/Makefile srpm outdir=<directory>
#
# It builds the source package from the checked-out tree — the version comes
# from pyproject.toml, so there is no second place holding a version number.
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
