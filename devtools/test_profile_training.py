"""Cheap boundary checks for the opt-in developer harness; no Qt/images."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devtools import profile_training as tool


class ProfileTrainingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kirby2-profile-test-')
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def run_tool(self, operation='export', source=None, name='out', profile=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.run(operation, source or self.source, self.base / name, profile)

    def test_profile_and_cold_folder_recovery_have_text_results(self):
        exported = self.run_tool(profile=True)
        recovered = self.run_tool('recover', self.base / 'out/export', 'recovery')
        self.assertEqual(exported['status'], 'PASS')
        self.assertEqual(recovered['status'], 'PASS')
        self.assertEqual(exported['result']['export_id'], recovered['result']['export_id'])
        self.assertFalse(recovered['result']['live_authority_restored'])
        self.assertFalse(exported['qt_imported'])
        self.assertEqual(list(self.source.iterdir()), [])
        self.assertTrue((self.base / 'out/profile.pstats').is_file())
        self.assertIn('SORTED BY cumulative', (self.base / 'out/profile.txt').read_text())
        events = [json.loads(line) for line in (self.base / 'out/events.jsonl').read_text().splitlines()]
        self.assertEqual(events[-1]['event'], 'operation_finished')
        self.assertEqual({e['operation_id'] for e in events}, {exported['operation_id']})
        self.assertEqual(sum(e['event'] == 'phase_started' for e in events),
                         sum(e['event'] == 'phase_finished' for e in events))
        self.assertTrue(all(p.suffix in {'.json', '.jsonl', '.pstats', '.txt'}
                            for p in (self.base / 'out').rglob('*') if p.is_file()))

    def test_existing_or_nested_output_never_overwrites_input(self):
        for output in (self.source, self.source / 'nested', self.base):
            with self.assertRaises((ValueError, FileExistsError)):
                tool.run('export', self.source, output)
        self.assertEqual(list(self.source.iterdir()), [])

    def test_owner_failure_records_error_and_terminal_status(self):
        with patch.object(tool.daily, 'export_training', side_effect=ValueError('deliberate refusal')):
            with self.assertRaisesRegex(ValueError, 'deliberate refusal'):
                self.run_tool(profile=True)
        result = json.loads((self.base / 'out/summary.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertFalse((self.base / 'out/export').exists())

    def test_preparation_failure_still_records_original_failure(self):
        with patch.object(tool.daily, '_copy', side_effect=ValueError('invalid input')):
            with self.assertRaisesRegex(ValueError, 'invalid input'):
                self.run_tool(profile=True)
        result = json.loads((self.base / 'out/summary.json').read_text())
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(result['error'], 'invalid input')

    def test_source_change_cannot_be_a_pass(self):
        original = tool.source_state()
        changed = dict(original, sha256='changed')
        with patch.object(tool, 'source_state', side_effect=[original, changed]):
            result = self.run_tool()
        self.assertEqual(result['status'], 'UNSETTLED_SOURCE_CHANGED')
