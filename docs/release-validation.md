# Initial source release validation

The source reorganization was checked offline before publication:

- 161 Python regression tests: 21 collection/configuration, 83 dataset, 48 rendering/supervision and 9 native setup tests.
- Six additional native UI image checks; Python compilation and Ruff static checks.
- Locked .NET builds of the extractor, native scene inspector and optional vehicle-control probe, with no warnings or errors.
- 49 extractor checks, seven native timing checks, six source-bound checks and six map-semantics mutation checks.
- Re-extraction of five original pilot ghosts produced 30 byte-identical output files. Two original partial-coverage ghosts produced 12 byte-identical output files under an explicit temporary source-bound policy, preserving unknown control intervals.
- Source/wheel packaging and a clean wheel installation outside the checkout, including CLI help and configuration discovery.

The original ghosts, generated outputs, machine paths and review approvals used for comparison are not included. The public tests use synthetic fixtures and do not require those private artifacts.

No new game rendering was performed for this release. The source port still requires the documented native pilot on each installation. Durable supervisor unit tests simulate process-control capabilities; actual supervision requires Linux Python with `os.pidfd_open` and `signal.pidfd_send_signal`, shown by `tmbench doctor`.
