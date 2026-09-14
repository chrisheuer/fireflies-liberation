---
name: Bug report
about: Something behaved differently than the docs say
labels: bug
---

**What happened, and what you expected instead**

**Which piece**
- [ ] `fireflies_dump.py` (getting data out)
- [ ] `datalib_source/` (the datalib import)

**Steps to reproduce** — the exact command, with any secret removed.

**Output** — the NDJSON `log` and `outcome` lines, or the traceback.
Please check it for meeting content before pasting; transcripts are
private, and an issue is public.

**Environment**
- OS and Python version:
- datalib version (`datalib-dag --version`), if relevant:
- `datalib-doltlite` on PATH (`which datalib-doltlite`), if relevant:

**Did it fail loudly or quietly?** If a step reported success and the
data was still wrong, say so — that class of bug is the one we most want
to hear about, and the hardest to notice.
