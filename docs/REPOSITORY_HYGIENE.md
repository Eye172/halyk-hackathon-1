# Dataset publication boundary - 2026-10-12

This copy retains the implementation and a small public-challenge example.
The following were removed from every reachable commit and branch:

- Complete public and hidden challenge directories.
- Submission outputs, `artifacts/`, `tmp/`, extracted text and OCR caches.
- The full calibration answer/fact pack and the private-submission helper.

All four original commits are retained, with their authors, dates, messages and
parent relationships. Before documentation/sample changes, remaining files were
verified byte-for-byte against the original archive. Commit IDs changed.
The original mirror is backed up locally and must not be pushed or merged back.

Only `examples/covenant-sample/` is the distributed dataset: two original PDFs,
four ledger rows and one reference answer from the public challenge. It does not
contain hidden-stage material or the full benchmark. `python tools/run_sample.py`
replays the single calculation through the implementation, without API calls.

Full datasets and their calibration pack can still be supplied locally and are
ignored by Git. The LLM mode remains available for local data. Migration documents
in `docs/migration/` describe the pre-cleanup archive, not the current file tree.

Run `python tools/check_repository_hygiene.py` before publishing or merging old
work. The CI check also examines reachable history. Re-clone after the rewrite;
do not merge a pre-cleanup history. Repository visibility is unchanged.
