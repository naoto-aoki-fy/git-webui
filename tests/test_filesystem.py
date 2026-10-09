import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from filesystem import browse_directories, prepare_location, resolve_location


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.default = self.root / 'clones'

    def test_default_and_custom_locations(self):
        self.assertEqual(resolve_location('', self.default), self.default)
        custom = self.root / 'folder with spaces'
        self.assertEqual(prepare_location(str(custom), self.default), custom)
        self.assertTrue(custom.is_dir())
        nested = custom / 'new' / 'nested'
        self.assertEqual(prepare_location(str(nested), self.default), nested)
        self.assertTrue(nested.is_dir())

    def test_relative_paths_and_home_expansion(self):
        self.assertEqual(resolve_location('.', self.default), Path.cwd())
        self.assertEqual(resolve_location('~', self.default), Path.home())

    def test_invalid_paths(self):
        for value in [12, None, [], 'bad\x00path', 'bad\npath', 'x' * 4097]:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(ValueError):
                resolve_location(value, self.default)
        file = self.root / 'file.txt'
        file.touch()
        with self.assertRaises(OSError):
            prepare_location(str(file), self.default)
        with self.assertRaises(NotADirectoryError):
            browse_directories(str(file), self.default)
        with self.assertRaises(NotADirectoryError):
            browse_directories(str(self.root / 'missing'), self.default)

    def test_browser_lists_only_directories_in_sorted_order(self):
        for name in ['Zulu', 'alpha', 'folder with spaces', '.hidden']:
            (self.root / name).mkdir()
        (self.root / 'file.txt').touch()
        result = browse_directories(str(self.root), self.default)
        self.assertEqual(result['path'], str(self.root))
        self.assertEqual(result['parent'], str(self.root.parent))
        self.assertEqual(result['default_location'], str(self.default))
        self.assertEqual([item['name'] for item in result['directories']],
                         ['.hidden', 'alpha', 'folder with spaces', 'Zulu'])
        for item in result['directories']:
            self.assertEqual(Path(item['path']).parent, self.root)

    def test_uncreated_default_browses_existing_parent_without_creating_it(self):
        result = browse_directories('', self.default)
        self.assertEqual(result['path'], str(self.root))
        self.assertFalse(self.default.exists())

    def test_filesystem_root_has_no_parent(self):
        result = browse_directories(str(Path.cwd().anchor), self.default)
        self.assertIsNone(result['parent'])

    def test_permission_errors_are_preserved(self):
        with patch.object(Path, 'iterdir', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                browse_directories(str(self.root), self.default)
        with patch.object(Path, 'mkdir', side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                prepare_location('', self.default)
