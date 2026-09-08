"""Die Blackbox-Skripte unter `scripts/` sind Standalone-Werkzeuge, keine Tests.

`split_test.py` passt zufällig auf pytests Default-Muster `*_test.py` und würde sonst
importiert — es hängt an absoluten Pfaden der Cold-Test-Umgebung, die es hier nicht gibt.
Was von den Skripten automatisiert ist, steht in `test_cold_suite.py` (docs/TESTING.md §6).
"""

from __future__ import annotations

collect_ignore_glob = ["scripts/*"]
