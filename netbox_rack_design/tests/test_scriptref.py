"""``scriptref.resolve_callable``: plain dotted paths and ``scriptmodule:`` storage refs.

The storage cases point ``STORAGES["scripts"]`` at a temporary directory, so
they exercise the same ``storages["scripts"].open()`` call a remote backend
(S3) would get.
"""

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from unittest import mock

from django.test import SimpleTestCase, override_settings

from netbox_rack_design import scriptref


def _scripts_storage(location):
    return {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        "scripts": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": location},
        },
    }


@contextmanager
def script_storage():
    """Point ``STORAGES["scripts"]`` at a temp dir; yield ``write(name, source)``.

    A context manager rather than ``enable()`` + ``addCleanup``: a cleanup runs
    after a method-level ``@override_settings`` has already been undone, and
    undoing overrides out of order restores the wrong snapshot -- the test's
    ``PLUGINS_CONFIG`` then leaks into every later test.
    """
    scripts_dir = tempfile.mkdtemp(prefix="rd-scripts-")

    def write(name, source):
        with open(os.path.join(scripts_dir, f"{name}.py"), "w") as fh:
            fh.write(source)

    scriptref.clear_cache()
    try:
        with override_settings(STORAGES=_scripts_storage(scripts_dir)):
            yield write
    finally:
        scriptref.clear_cache()
        shutil.rmtree(scripts_dir, ignore_errors=True)


class DottedPathTest(SimpleTestCase):
    def test_dotted_path_still_imports(self):
        self.assertIs(scriptref.resolve_callable("os.path.join", "naming_script"), os.path.join)

    def test_bad_dotted_path_message(self):
        with self.assertRaisesMessage(ValueError, "Could not import naming_script 'no.such.fn'"):
            scriptref.resolve_callable("no.such.fn", "naming_script")

    def test_not_callable(self):
        with self.assertRaisesMessage(ValueError, "is not callable"):
            scriptref.resolve_callable("os.sep", "distribution_script")


class ScriptModuleTest(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.write_script = self.enterContext(script_storage())

    def test_loads_function_from_script_storage(self):
        self.write_script("device_naming", "def build_name(p):\n    return f'srv-{p}'\n")
        fn = scriptref.resolve_callable("scriptmodule:device_naming.build_name", "naming_script")
        self.assertEqual(fn(7), "srv-7")

    def test_missing_module(self):
        with self.assertRaisesMessage(ValueError, "Could not read naming_script module 'nope.py'"):
            scriptref.resolve_callable("scriptmodule:nope.build_name", "naming_script")

    def test_missing_function(self):
        self.write_script("device_naming", "x = 1\n")
        with self.assertRaisesMessage(ValueError, "has no 'build_name'"):
            scriptref.resolve_callable("scriptmodule:device_naming.build_name", "naming_script")

    def test_ref_without_function(self):
        with self.assertRaisesMessage(ValueError, "must name a module and a function"):
            scriptref.resolve_callable("scriptmodule:device_naming", "naming_script")

    def test_not_callable(self):
        self.write_script("device_naming", "build_name = 'x'\n")
        with self.assertRaisesMessage(ValueError, "is not callable"):
            scriptref.resolve_callable("scriptmodule:device_naming.build_name", "naming_script")

    def test_cached_within_recheck_window(self):
        self.write_script("device_naming", "def build_name(p):\n    return 'v1'\n")
        ref = "scriptmodule:device_naming.build_name"
        self.assertEqual(scriptref.resolve_callable(ref, "naming_script")(None), "v1")
        self.write_script("device_naming", "def build_name(p):\n    return 'v2'\n")
        self.assertEqual(scriptref.resolve_callable(ref, "naming_script")(None), "v1")

    def test_edit_picked_up_after_recheck_window(self):
        self.write_script("device_naming", "def build_name(p):\n    return 'v1'\n")
        ref = "scriptmodule:device_naming.build_name"
        with mock.patch.object(scriptref, "RECHECK_SECONDS", 0):
            self.assertEqual(scriptref.resolve_callable(ref, "naming_script")(None), "v1")
            self.write_script("device_naming", "def build_name(p):\n    return 'v2'\n")
            self.assertEqual(scriptref.resolve_callable(ref, "naming_script")(None), "v2")

    def test_unchanged_file_is_not_re_executed(self):
        self.write_script("counter", "RUNS = []\nRUNS.append(1)\ndef f():\n    return len(RUNS)\n")
        ref = "scriptmodule:counter.f"
        with mock.patch.object(scriptref, "RECHECK_SECONDS", 0):
            scriptref.resolve_callable(ref, "naming_script")
            self.assertEqual(scriptref.resolve_callable(ref, "naming_script")(), 1)

    def test_core_app_name_is_not_shadowed(self):
        import circuits

        self.write_script("circuits", "def f():\n    return 'mine'\n")
        fn = scriptref.resolve_callable("scriptmodule:circuits.f", "naming_script")
        self.assertEqual(fn(), "mine")
        self.assertIs(sys.modules["circuits"], circuits)
