"""Test suite for Morning Brief.

Stdlib `unittest` on purpose: the whole point of this project is that it runs
on a clean Python install, so the tests must not add a dependency the app
itself doesn't have.

Run them from the project root:

    python -m unittest discover -s tests -t .

or use run-tests.ps1, which does the same thing and summarises the result.

Nothing in here touches the network or the real library/calendar files - every
test that would write to disk redirects the module's path constants at a
temporary directory first.
"""
