"""Headless-Chrome (Playwright) end-to-end regression tests for the rack editor.

These tests drive the editor's CLIENT-SIDE JavaScript (GridStack tiles, the ×
matrix, live move ghosts, palette adds) against a LIVE dev server — the part the
pure-Python ``manage.py test`` suite cannot exercise. They are intentionally NOT
collected by ``manage.py test`` (which only walks ``netbox_rack_design``); run
them on demand via ``dev/e2e.sh``.

STRICTLY READ-ONLY: the suite never clicks Save / POSTs to save-layout, so it
never mutates design 5 or any real data. It asserts on in-page DOM state and on
the payload ``buildRackPayload`` WOULD send (reconstructed faithfully from the
same live DOM + GridStack node state the editor reads), never persisting.
"""
