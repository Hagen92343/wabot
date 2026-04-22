"""whatsbot Pre-Tool hook package.

Separate from the ``whatsbot`` package so Claude can invoke the hook
script without importing the full bot. ``pre_tool.py`` is the
entrypoint registered in each project's ``.claude/settings.json``.
"""
