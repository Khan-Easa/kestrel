"""Phase 7 substep 4: operator-facing CLI tools.

Currently contains ``keys`` (the ``kestrel-keys`` console-script entry).
Future substeps may add ``admin`` or similar — kept in its own subpackage
so the FastAPI layer never accidentally imports CLI code.
"""