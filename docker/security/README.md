# Retired container vulnerability scanner bridge

The production image uses the unmodified, official, digest-pinned Python
3.14.7 slim Trixie image. It applies no local interpreter source patch.

`python-3.14.7-grype-db-bridge.openvex.json` records three fixes already present
in Python 3.14.7 which an earlier Grype database still attributed only to a later
Python version. Each statement is scoped to `pkg:generic/python@3.14.7` and binds
the former official multi-architecture image digest and its verified native
standard-library postimage.

The current Grype database no longer reports those findings, so neither the
complete report nor the actionable gate consumes this file. It is retained only
as an auditable record of the retired exception and must not be re-enabled or
extended for unrelated scanner findings. Remove it in a separately reviewed
cleanup when historical-file deletion is authorized.
