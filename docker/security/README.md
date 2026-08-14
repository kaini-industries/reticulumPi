# Container vulnerability scanner bridge

The production image uses the unmodified, official, digest-pinned Python
3.14.7 slim Trixie image. It applies no local interpreter source patch.

`python-3.14.7-grype-db-bridge.openvex.json` records three fixes already
present in that stable interpreter which Grype 0.110.0 database schema 6.1.9,
built `2026-08-08T06:22:53Z`, still attributes only to a later Python version.
Each statement is scoped to `pkg:generic/python@3.14.7` and binds the official
multi-architecture image digest and the verified native standard-library
postimage. The complete JSON vulnerability report is generated and uploaded
without VEX; only the subsequent actionable high-severity gate reads this
bridge.

Remove each statement as soon as the current Grype database recognizes Python
3.14.7 as fixed for that CVE. Remove the document and workflow input when no
statements remain. Do not convert unrelated or unverified scanner findings
into this bridge.
