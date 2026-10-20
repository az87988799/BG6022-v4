"""Bounded offline document windows; no scientific execution or external GET."""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from orca_agent.context import _document_excerpt_view
from orca_agent.tools import knowledge


def test_code_line_breaks_survive_html_but_ordinary_prose_stays_normalized():
    parser = knowledge._Text()
    parser.feed('<article><p>A\n  paragraph with <code>inline</code> text.</p>'
                '<pre><code>%block\n  option true # comment\n  second 2<br>end\n</code></pre>'
                '<p><code>first\nsecond</code></p></article>')
    assert parser.parts == ['A paragraph with inline text.',
                           '%block\n  option true # comment\n  second 2\nend', 'first\nsecond']


def test_long_code_query_reaches_later_declarations_and_records_exact_continuous_range():
    code = '%block\n' + '# unrelated explanation\n' * 130 + '  Nuclei = all X { flags }\n  Shielding true\nend\n'
    parser = knowledge._Text()
    parser.feed('<article><pre>' + code + '</pre></article>')
    windows = []
    excerpt, indices = knowledge.matched_excerpt(parser.parts, 'Nuclei shielding syntax', windows=windows)
    assert indices == [0] and '  Nuclei = all X { flags }\n  Shielding true\nend' in excerpt
    window, = windows
    assert code.index('Nuclei =') > 1800 and window['start_character'] > 0 and window['truncated_before']
    assert not window['partial_line']
    assert excerpt == parser.parts[0][window['start_character']:window['end_character']]
    assert len(excerpt) <= 1800
    assert knowledge.matched_excerpt(parser.parts, 'Nuclei shielding syntax') == (excerpt, indices)
    full, _ = knowledge.matched_excerpt(parser.parts, 'irrelevant', paragraph=0,
                                        start_character=window['start_character'])
    assert full == excerpt


def test_long_single_line_uses_richer_later_hits_and_character_continuation():
    text = 'ORCA ' + 'background ' * 300 + 'nuclei shielding syntax end' + ' trailing' * 220
    excerpt, window = knowledge._document_window(text, {'orca', 'nuclei', 'shielding'})
    assert 'nuclei shielding' in excerpt and window['start_character'] > 0
    assert excerpt == text[window['start_character']:window['end_character']]
    assert window['partial_line'] and window['truncated_after']
    following, continuation = knowledge._document_window(text, set(), window['end_character'])
    assert following == text[window['end_character']:continuation['end_character']]
    assert continuation['start_character'] == window['end_character']


@pytest.mark.parametrize('parameters', [
    {'start_character': 1}, {'paragraph': -1}, {'paragraph': 1048577},
    {'paragraph': 0, 'start_character': -1}, {'paragraph': 0, 'start_character': 1048577},
])
def test_window_schema_refuses_unscoped_or_unbounded_offsets(parameters):
    with pytest.raises(ValidationError):
        knowledge.SearchParameters(document='orca_input', query='syntax', **parameters)


def test_runtime_document_bounds_refuse_nonexistent_paragraph_or_character():
    with pytest.raises(ValueError, match='paragraph'):
        knowledge.matched_excerpt(['text'], 'text', paragraph=1)
    with pytest.raises(ValueError, match='start_character'):
        knowledge.matched_excerpt(['text'], 'text', paragraph=0, start_character=4)
    windows = []
    excerpt, _ = knowledge.matched_excerpt(['syntax ' + 'x' * 2000] * 3, 'syntax', windows=windows)
    assert len(excerpt) <= 5400 and len(windows) == 3
    assert all(row['end_character'] - row['start_character'] <= 1800 for row in windows)


def document(excerpt):
    return {'status': 'found', 'document_id': 'orca:index.html', 'document_sha256': 'a' * 64,
            'excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest(), 'excerpt': excerpt,
            'query': 'Nuclei shielding syntax', 'coverage': {'complete': False, 'paragraph_indices': [121]},
            'document_artifact_id': 'official_document', 'scientific_qualification': False,
            'source': {'duplicated_receipt': 'x' * 5000},
            'manual_references': ['orca:contents/' + 'unrelated' * 12 + str(i) + '.html' for i in range(127)]
                                 + ['orca:contents/shielding.html']}


def test_navigation_and_duplicate_receipts_cannot_hide_new_query_excerpt():
    original = document('intro\n' * 300 + 'Nuclei = all X { flags }\nend\n')
    before = copy.deepcopy(original)
    assert len(json.dumps(original).encode()) > 12000
    result = _document_excerpt_view(original)
    assert result['excerpt'] == original['excerpt']
    assert result['document_sha256'] == original['document_sha256']
    assert result['excerpt_sha256'] == original['excerpt_sha256']
    assert result['document_artifact_id'] == 'official_document'
    assert 'orca:contents/shielding.html' in result['manual_references']
    assert result['manual_references_projection']['omitted'] == 120
    assert len(json.dumps(result).encode()) <= 12000 and original == before


def test_multibyte_projection_keeps_query_window_and_marks_original_excerpt_coverage():
    original = document('前言' * 2400 + '\nNuclei shielding syntax\n')
    result = _document_excerpt_view(original)
    assert 'Nuclei shielding syntax' in result['excerpt']
    assert result['excerpt_projection']['complete'] is False
    assert result['excerpt_projection']['original_sha256'] == original['excerpt_sha256']
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 12000


def test_related_navigation_after_original_first_128_links_remains_discoverable():
    links = knowledge._ManualLinks(knowledge.MANUAL_ROOT + 'index.html', 'shielding')
    links.feed(''.join(f'<a href="contents/other{i}.html">link</a>' for i in range(130))
               + '<a href="contents/shielding.html">match</a>'
               + '<a href="https://untrusted.invalid/shielding.html">foreign</a>')
    assert len(links.links) == 128
    assert 'orca:contents/shielding.html' in links.links
    assert not any('untrusted' in link for link in links.links)
