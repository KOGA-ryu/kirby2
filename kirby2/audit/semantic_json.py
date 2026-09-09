"""Canonical text boundary regression for the ASCII validation fast path."""
import json
from types import MappingProxyType
import unittest

from kirby2.full_day.models import canonical_json_bytes, parse_canonical_json_object, validate_strict_json


class SemanticJsonAcceptance(unittest.TestCase):
    def test_every_ascii_character_in_values_and_keys_preserves_wire_bytes(self):
        text = ''.join(chr(i) for i in range(128))
        record = {chr(i): [text, i, None, True, False] for i in range(128)}
        expected = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
        self.assertIs(validate_strict_json(record), record)
        self.assertEqual(canonical_json_bytes(record), expected)
        self.assertEqual(parse_canonical_json_object(expected), record)

    def test_non_ascii_nfc_and_scalar_boundaries_remain_accepted(self):
        texts = ['', '\x7f', '\x80', '\u00e9', '\u4e2d', '\ud7ff', '\ue000', '\uffff', '\U00010000', '\U0010ffff']
        record = {text: texts for text in texts}
        expected = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        self.assertEqual(canonical_json_bytes(record), expected)
        self.assertEqual(parse_canonical_json_object(expected), record)

    def test_surrogate_values_and_keys_still_refuse(self):
        for scalar in (0xD800, 0xDBFF, 0xDC00, 0xDFFF):
            for text in (chr(scalar), 'ascii'+chr(scalar), '\u00e9'+chr(scalar)):
                for record in ({'ascii': [text]}, {'ascii': 1, text: 2}):
                    with self.subTest(scalar=scalar, record=repr(record)):
                        with self.assertRaisesRegex(ValueError, 'Unicode scalar'):
                            canonical_json_bytes(record)

    def test_non_nfc_values_and_keys_still_refuse(self):
        for text in ('e\u0301', 'A\u030a', '\u212b', 'ascii-e\u0301'):
            for record in ({'ascii': text}, {'ascii': 1, text: 2}):
                with self.subTest(record=repr(record)):
                    with self.assertRaisesRegex(ValueError, 'NFC'):
                        canonical_json_bytes(record)

    def test_key_type_validation_precedes_unicode_validation(self):
        class Text(str):
            pass
        for key in (1, False, None, b'ascii', Text('ascii')):
            with self.assertRaisesRegex(TypeError, 'keys must be strings'):
                canonical_json_bytes({'\ud800': 0, key: 1})

    def test_cycles_and_unsupported_values_cannot_hide_behind_ascii(self):
        record = {'ascii': []}
        record['ascii'].append(record)
        with self.assertRaisesRegex(ValueError, 'cycles'):
            canonical_json_bytes(record)
        for value in (1.0, float('nan'), float('inf'), b'ascii', {'a'}, object()):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(TypeError):
                    canonical_json_bytes({'ascii': value})

    def test_shared_acyclic_containers_and_read_only_mappings_remain_valid(self):
        shared = MappingProxyType({'ascii': ('\u00e9', 10**60)})
        self.assertEqual(canonical_json_bytes([shared, shared]),
                         canonical_json_bytes([{'ascii': ['\u00e9', 10**60]}]*2))

    def test_wire_still_rejects_noncanonical_and_duplicate_keys(self):
        for raw in (b'{"a":1,"a":2}', b'{"a": 1}', b'{"z":1,"a":2}', b'{"a":1.0}'):
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError, TypeError)):
                    parse_canonical_json_object(raw)
