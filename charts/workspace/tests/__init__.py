"""Marks the workspace test suite as a regular package.

Without this file `tests/` is only a namespace *portion*, and the import
system treats those differently from regular packages: the path finder
records a namespace portion and keeps scanning sys.path, whereas a directory
carrying `__init__.py` is returned immediately. Any environment with a real
`tests` package installed therefore shadowed this one outright — even though
`charts/workspace` sits at sys.path[0] during a test run — and the suite
failed with `cannot import name 'http_harness' from 'tests'` pointing at a
wholly unrelated project.

That is not hypothetical: an editable install elsewhere on the machine cost a
debugging session, silently hid 843 tests behind 26 loader errors, and made a
green suite look broken. The modules here already import each other as
`tests.<name>`, so being a real package is what the suite assumed anyway.
"""
