# Container security backports

The production container uses a supported, digest-pinned Python image. When a
high-severity interpreter issue has an upstream fix but no patched stable image
yet, a temporary backport may live in this directory only when all of the
following are true:

- the change comes from a verified upstream CPython security commit;
- the patcher verifies the complete source preimage and postimage hashes;
- the container build fails closed if the pinned image changes;
- a VEX statement identifies the exact patched component and vulnerability;
- focused tests and the container vulnerability gate validate the backport.

`patch_cpython_html_parser.py` applies the Python 3.14 backport from CPython
commit `07efb08123ba9367a7107325adb9d5626dca1ca9` for CVE-2026-15308. The
corresponding VEX statement is scoped to `pkg:generic/python@3.14.6`.

Remove both the patch and its VEX statement as soon as the reviewed base-image
digest contains a stable Python release with the upstream fix. CPython-derived
patch content remains subject to the Python Software Foundation License.
