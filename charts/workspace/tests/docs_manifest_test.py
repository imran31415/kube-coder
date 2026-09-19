"""The REAL docs/_manifest.json points only at files that exist.

docs_api_test builds a throwaway manifest to exercise the endpoints; nothing
checked the one that ships. A page added to the manifest without its file (or
a file renamed under it) is a dead link in the in-app docs and a 404 from the
New Build form's "What's this?" (#701).

Run:  python3 -m unittest tests.docs_manifest_test   (from charts/workspace/)
"""

import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.realpath(os.path.join(HERE, '..', '..', '..', 'docs'))


class ShippedManifestTests(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(DOCS, '_manifest.json'), encoding='utf-8') as f:
            self.manifest = json.load(f)

    def pages(self):
        for section in self.manifest['sections']:
            for page in section['pages']:
                yield section['id'], page

    def test_every_page_file_exists(self):
        missing = [f'{sid}/{p["id"]} -> {p["file"]}' for sid, p in self.pages()
                   if not os.path.isfile(os.path.join(DOCS, p['file']))]
        self.assertEqual(missing, [])

    def test_page_ids_are_unique(self):
        ids = [p['id'] for _sid, p in self.pages()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_worktree_guide_is_listed(self):
        self.assertIn('builds-worktrees', [p['id'] for _sid, p in self.pages()])


if __name__ == '__main__':
    unittest.main()
