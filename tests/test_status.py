import plistlib
import subprocess

import pytest

import server

OBSERVED = {
    'printer-state': 3,
    'printer-state-reasons': 'media-empty-report',
    'marker-names': ['Black ink', 'Cyan ink', 'Magenta ink', 'Yellow ink'],
    'marker-levels': [28, 72, 70, 72],
    'marker-types': ['ink'] * 4,
    'marker-colors': ['#000000', '#00FFFF', '#FF00FF', '#FFFF00'],
    'media-ready': 'na_letter_8.5x11in',
    'printer-input-tray': [b'type=other;level=-2;status=0;name=Sheet feeder bin 1;'],
}


def reply(attributes):
    return subprocess.CompletedProcess([], 0, plistlib.dumps({
        'Successful': True, 'Tests': [{'Successful': True, 'StatusCode': 'successful-ok',
                                     'ResponseAttributes': [attributes]}],
    }).decode(), '')


def test_observed_estimate_and_paper_reason_are_qualified(cups, monkeypatch):
    monkeypatch.setenv('PRINT_STATUS_URI', 'ipp://localhost:60000/ipp/print')
    monkeypatch.setenv('PRINT_INK_LEVELS_ESTIMATED', '1')
    monkeypatch.setenv('PRINT_MEDIA_REASONS_UNRELIABLE', '1')
    cups.side_effect = [subprocess.CompletedProcess([], 0, 'printer epson is idle.', ''),
                        reply(OBSERVED), reply(OBSERVED)]
    status = server.printer_status()
    assert status['source'] == 'printer'
    black = status['supplies'][0]
    assert black['name'] == 'Black ink' and black['level_percent'] == 28
    assert black['accuracy'] == 'estimated by printer, may be stale; visually check tank'
    assert status['reasons'][0]['reason'] == 'media-empty-report'
    assert 'does not confirm an empty tray' in status['reasons'][0]['note']
    assert status['input_trays'][0]['level_known'] is False
    assert status['media_ready'] == ['na_letter_8.5x11in']
    assert status['cups']['source'] == 'cups-cache'
    assert cups.call_args.args[0][-2] == 'ipp://localhost:60000/ipp/print'


def test_marker_order_and_embedded_commas_are_preserved():
    attrs = {'marker-names': ['Cyan, primary', 'Black'], 'marker-levels': [72, 28],
             'marker-types': ['ink', 'ink'], 'marker-colors': ['#00FFFF', '#000000']}
    parsed = server._status_attributes(reply(attrs).stdout)
    status = server._summarize_status(parsed, 'printer')
    assert [(m['name'], m['level_percent'], m['color']) for m in status['supplies']] == [
        ('Cyan, primary', 72, '#00FFFF'), ('Black', 28, '#000000')]
    assert status['marker_mapping_reliable']
    assert 'accuracy unknown' in status['supplies'][0]['accuracy']


def test_single_marker_and_missing_optional_metadata():
    status = server._summarize_status({'marker-names': 'Black', 'marker-levels': 28}, 'printer')
    assert status['supplies'][0]['level_percent'] == 28
    assert status['supplies'][0]['color'] is None
    assert status['supplies'][0]['type'] is None


@pytest.mark.parametrize('levels', [[28], [28, 72, 70], None])
def test_unequal_arrays_do_not_guess_color_level_mapping(levels):
    status = server._summarize_status({'marker-names': ['Black', 'Cyan'],
                                       'marker-levels': levels}, 'printer')
    assert not status['marker_mapping_reliable']
    assert all(m['level_percent'] is None and m['reported_level'] is None for m in status['supplies'])
    assert any('cannot be assigned safely' in note for note in status['notes'])


@pytest.mark.parametrize('level,meaning', [
    (-1, 'unavailable'), (-2, 'unknown'), (-3, 'unknown, not yet at capacity'),
    (101, 'unknown'), ('28', 'unknown'), (True, 'unknown'),
])
def test_non_percent_levels_are_not_empty_or_a_percentage(level, meaning):
    status = server._summarize_status({'marker-names': 'Black', 'marker-levels': level}, 'printer')
    assert status['supplies'][0]['level_percent'] is None
    assert status['supplies'][0]['level_status'] == meaning


def test_no_markers_and_unknown_trays_are_explicit():
    status = server._summarize_status({'printer-input-tray': [
        b'level=-2;status=19;name=Rear Auto Sheet Feeder;', b'level=10;name=Main;']}, 'printer')
    assert not status['markers_reported'] and status['supplies'] == []
    assert status['input_trays'][0]['level_known'] is False
    assert status['input_trays'][1]['level_known'] is True
    assert status['input_trays'][0]['reported']['status'] == '19'


def test_fresh_values_override_cache_and_keep_sources_separate(cups, monkeypatch):
    monkeypatch.setenv('PRINT_STATUS_URI', 'ipp://localhost:60000/ipp/print')
    cups.side_effect = [subprocess.CompletedProcess([], 0, 'idle', ''),
                        reply(OBSERVED), reply({**OBSERVED, 'marker-levels': [51, 72, 70, 72]})]
    status = server.printer_status()
    assert status['supplies'][0]['level_percent'] == 51
    assert status['cups']['supplies'][0]['level_percent'] == 28
    assert status['fresh'] is True and status['source'] == 'printer'
    commands = [call.args[0] for call in cups.call_args_list]
    assert commands[1][1:4] == ('-T', '10', '-X')
    assert commands[1][-2] == 'ipp://localhost:631/printers/epson'


def test_unreachable_printer_falls_back_to_labeled_cache(cups, monkeypatch):
    monkeypatch.setenv('PRINT_STATUS_URI', 'ipp://localhost:60000/ipp/print')
    cups.side_effect = [subprocess.CompletedProcess([], 0, 'idle', ''), reply(OBSERVED),
                        FileNotFoundError('private details')]
    status = server.printer_status()
    assert status['source'] == 'cups-cache' and status['fresh'] is False
    assert 'private' not in str(status)
    assert any('cached' in note for note in status['notes'])


def test_direct_status_survives_cups_failure(cups, monkeypatch):
    monkeypatch.setenv('PRINT_STATUS_URI', 'ipp://localhost:60000/ipp/print')
    cups.side_effect = [FileNotFoundError(), FileNotFoundError(), reply(OBSERVED)]
    status = server.printer_status()
    assert status['fresh'] is True and status['source'] == 'printer'
    assert 'CUPS' in status['ipp_error']


@pytest.mark.parametrize('text', ['not XML', '<plist>',
    plistlib.dumps({'Successful': False, 'Tests': []}).decode()])
def test_malformed_or_failed_status_is_not_successful(text):
    with pytest.raises(ValueError, match='Invalid or unsuccessful'):
        server._status_attributes(text)


def test_missing_printer_configuration_never_guesses_a_device(cups):
    cups.side_effect = [subprocess.CompletedProcess([], 0, 'idle', ''), reply(OBSERVED)]
    status = server.printer_status()
    assert status['source'] == 'cups-cache'
    assert cups.call_count == 2


def test_quirk_flags_do_not_hide_real_reasons_or_change_levels(monkeypatch):
    monkeypatch.setenv('PRINT_INK_LEVELS_ESTIMATED', '1')
    monkeypatch.setenv('PRINT_MEDIA_REASONS_UNRELIABLE', '1')
    status = server._summarize_status({**OBSERVED, 'printer-state-reasons': [
        'media-empty-report', 'media-jam-error']}, 'printer')
    assert status['supplies'][0]['level_percent'] == 28
    assert [r['reason'] for r in status['reasons']] == ['media-empty-report', 'media-jam-error']
    assert 'sticky' in status['reasons'][0]['note']


def test_invalid_status_uri_never_executes(cups):
    with pytest.raises(ValueError):
        server._query_status('http://user:secret@localhost:60000/ipp/print', 'printer')
    cups.assert_not_called()
