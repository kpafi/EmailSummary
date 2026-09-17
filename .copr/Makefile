# For an SCM package with build method "make_srpm", COPR calls exactly this
# target:
#
#     make -f .copr/Makefile srpm outdir=<directory>
#
# It builds the source package from the checked-out tree — the version comes
# from pyproject.toml, so there is no second place holding a version number.
#
# The tarball is assembled with tar, not with `git archive`: git is not in the
# SRPM buildroot, and a missing git at the left end of a pipe does not fail the
# shell -- the pipe returns gzip's status, gzip is happy with empty input, and
# the result is a valid 20-byte gzip stream holding nothing. That archive was
# uploaded and only recognised as rubbish in %prep, one build stage later.
# pipefail and the listing check below make that failure mode loud.
#
# The excludes are not cosmetic either: COPR builds from a fresh clone, a
# developer's tree also holds .venv, dist/ and state.db. Without them the
# archive's contents depend on where the build runs, which is the kind of
# difference that only shows up in the package nobody tested.
SHELL := /bin/bash

srpm:
	dnf -y install rpm-build tar sed
	set -euo pipefail; \
	version=$$(sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml | head -1); \
	test -n "$$version"; \
	echo "==> MailDigest $$version"; \
	rm -rf .srpm-build; \
	mkdir -p .srpm-build/SOURCES .srpm-build/SPECS ".srpm-build/stage/maildigest-$$version"; \
	tar --exclude=.git --exclude=.srpm-build --exclude=.venv --exclude=./dist \
	    --exclude=state.db --exclude=__pycache__ --exclude=.pytest_cache \
	    --exclude=.mypy_cache --exclude=.ruff_cache -cf - . \
	  | tar -xf - -C ".srpm-build/stage/maildigest-$$version"; \
	tar -czf ".srpm-build/SOURCES/maildigest-$$version.tar.gz" \
	  -C .srpm-build/stage "maildigest-$$version"; \
	listing="$$(tar -tzf ".srpm-build/SOURCES/maildigest-$$version.tar.gz")"; \
	grep -qx "maildigest-$$version/pyproject.toml" <<< "$$listing"; \
	echo "==> tarball: $$(stat -c%s ".srpm-build/SOURCES/maildigest-$$version.tar.gz") bytes, $$(wc -l <<< "$$listing") entries"; \
	sed "s/@VERSION@/$$version/g" packaging/rpm/maildigest.spec \
	  > .srpm-build/SPECS/maildigest.spec; \
	rpmbuild -bs .srpm-build/SPECS/maildigest.spec \
	  --define "_topdir $$(pwd)/.srpm-build" \
	  --define "_sourcedir $$(pwd)/.srpm-build/SOURCES" \
	  --define "_srcrpmdir $(outdir)"
