"""Provider failures cannot become invented outlines or completed report text."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import report_agent as reports


def _outline():
    return {'title': 'Renewal outlook', 'summary': 'Two participants negotiate their protections.',
            'sections': [{'title': 'Predicted outcome'}, {'title': 'Risks and uncertainty'}]}


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(reports, 'get_locale', lambda: 'en')
    monkeypatch.setattr(reports, 'get_language_instruction', lambda: 'Respond in English.')
    llm = Mock()
    llm.chat_json.return_value = _outline()
    tools = SimpleNamespace(get_simulation_context=lambda **_kwargs: {
        'graph_statistics': {'total_nodes': 2, 'total_edges': 3},
        'total_entities': 2, 'related_facts': ['The buyer requests a termination clause.'],
    })
    return reports.ReportAgent('graph-owned', 'sim-owned', 'Keep this report brief.', llm, tools)


def test_outline_preserves_model_content_and_uses_bounded_english_instructions(agent):
    result = agent.plan_outline()
    assert result.title == 'Renewal outlook'
    assert [s.title for s in result.sections] == ['Predicted outcome', 'Risks and uncertainty']
    call = agent.llm.chat_json.call_args.kwargs
    assert call['max_tokens'] == 1024
    assert call['response_schema'] is reports.REPORT_OUTLINE_SCHEMA
    assert call['messages'][0]['content'].startswith('You write future-scenario reports')
    assert 'two to five' in call['messages'][0]['content']
    assert 'Keep this report brief.' in call['messages'][1]['content']
    assert 'The buyer requests a termination clause.' in call['messages'][1]['content']


@pytest.mark.parametrize('response', [
    None, [], {}, {'title': '', 'summary': 'Summary', 'sections': _outline()['sections']},
    {**_outline(), 'summary': None}, {**_outline(), 'sections': []},
    {**_outline(), 'sections': [{'title': 'Only one'}]},
    {**_outline(), 'sections': [{'title': 'Too many'}] * 6},
    {**_outline(), 'sections': [{'title': 'Valid'}, {'title': '   '}]},
])
def test_invalid_outline_is_a_failure_instead_of_a_default(agent, response):
    agent.llm.chat_json.return_value = response
    with pytest.raises(RuntimeError, match='Report outline generation failed'):
        agent.plan_outline()
    agent.llm.chat_json.assert_called_once()
    agent.llm.chat.assert_not_called()


def test_failed_planning_persists_failure_and_never_generates_sections(agent, monkeypatch, tmp_path):
    sentinel = 'PRIVATE_PROVIDER_CREDENTIAL_AND_BODY'
    agent.llm.chat_json.side_effect = TimeoutError(sentinel)
    monkeypatch.setattr(reports.Config, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(reports.ReportManager, 'REPORTS_DIR', str(tmp_path / 'reports'))
    result = agent.generate_report(report_id='report-owned-failure')
    assert result.status == reports.ReportStatus.FAILED
    assert datetime.fromisoformat(result.completed_at)
    assert 'TimeoutError' in result.error and sentinel not in result.error
    agent.llm.chat_json.assert_called_once()
    agent.llm.chat.assert_not_called()
    folder = tmp_path / 'reports' / result.report_id
    assert json.loads((folder / 'meta.json').read_text())['status'] == 'failed'
    assert json.loads((folder / 'progress.json').read_text())['status'] == 'failed'
    assert not (folder / 'outline.json').exists()
    assert not (folder / 'full_report.md').exists()
    assert not list(folder.glob('section_*.md'))
    assert sentinel not in ''.join(p.read_text() for p in folder.iterdir() if p.is_file())


@pytest.mark.parametrize('response', [None, '', '   '])
def test_empty_section_response_fails_once_without_replaying_or_publishing(agent, response):
    agent.llm.chat.return_value = response
    outline = reports.ReportOutline('Title', 'Summary', [reports.ReportSection('One'), reports.ReportSection('Two')])
    with pytest.raises(RuntimeError, match='no content'):
        agent._generate_section_react(outline.sections[0], outline, [])
    agent.llm.chat.assert_called_once()
    assert agent.llm.chat.call_args.kwargs['max_tokens'] == 4096


def test_report_budgets_are_explicit_and_reject_invalid_values(monkeypatch, agent):
    monkeypatch.setenv('REPORT_PLAN_MAX_TOKENS', '768')
    monkeypatch.setenv('REPORT_SECTION_MAX_TOKENS', '1536')
    configured = reports.ReportAgent('graph-owned', 'sim-owned', 'Brief', agent.llm, agent.zep_tools)
    assert configured.plan_max_tokens == 768 and configured.section_max_tokens == 1536
    for name in ['REPORT_PLAN_MAX_TOKENS', 'REPORT_SECTION_MAX_TOKENS']:
        for bad in ['0', '255', '8193', 'nan']:
            with monkeypatch.context() as patch:
                patch.setenv(name, bad)
                with pytest.raises(ValueError):
                    reports.ReportAgent('graph-owned', 'sim-owned', 'Brief', agent.llm, agent.zep_tools)


@pytest.mark.parametrize('tz', [None, timezone.utc, timezone(timedelta(hours=5, minutes=30))])
def test_agent_log_clock_advances_without_new_rows(monkeypatch, tmp_path, tz):
    monkeypatch.setattr(reports.ReportManager, 'REPORTS_DIR', str(tmp_path))
    folder = tmp_path / 'report-clock'
    folder.mkdir()
    row = {'action': 'report_start', 'timestamp': (datetime.now(tz) - timedelta(seconds=125)).isoformat(),
           'elapsed_seconds': 0}
    (folder / 'agent_log.jsonl').write_text(json.dumps(row) + '\n')
    result = reports.ReportManager.get_agent_log('report-clock', from_line=1)
    assert result['logs'] == []
    assert result['total_lines'] == 1
    assert result['status'] == 'generating'
    assert 125 <= result['elapsed_seconds'] < 130


@pytest.mark.parametrize('terminal, expected', [
    ({'action': 'error', 'stage': 'failed', 'details': {'error': 'Provider timed out'}}, 'failed'),
    ({'action': 'report_complete', 'stage': 'completed'}, 'completed'),
])
def test_terminal_log_clock_and_failure_survive_incremental_reads(monkeypatch, tmp_path, terminal, expected):
    monkeypatch.setattr(reports.ReportManager, 'REPORTS_DIR', str(tmp_path))
    folder = tmp_path / 'report-terminal'
    folder.mkdir()
    rows = [{'action': 'report_start', 'timestamp': '2020-01-01T00:00:00', 'elapsed_seconds': 0},
            {**terminal, 'elapsed_seconds': 60.25}]
    (folder / 'agent_log.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    result = reports.ReportManager.get_agent_log('report-terminal', from_line=2)
    assert result['logs'] == []
    assert result['status'] == expected
    assert result['elapsed_seconds'] == 60.25
    assert result['error'] == ('Provider timed out' if expected == 'failed' else None)


def test_agent_log_retries_uncommitted_tail_without_skipping_or_duplicating(monkeypatch, tmp_path):
    monkeypatch.setattr(reports.ReportManager, 'REPORTS_DIR', str(tmp_path))
    folder = tmp_path / 'report-tail'
    folder.mkdir()
    path = folder / 'agent_log.jsonl'
    start = json.dumps({'action': 'report_start', 'timestamp': 'invalid'}) + '\n'
    final = json.dumps({'action': 'report_complete', 'elapsed_seconds': 5})
    path.write_text(start + 'invalid committed row\n' + final[:10])
    result = reports.ReportManager.get_agent_log('report-tail')
    assert result['total_lines'] == 2
    assert len(result['logs']) == 1
    path.write_text(start + 'invalid committed row\n' + final)
    assert reports.ReportManager.get_agent_log('report-tail', 2)['total_lines'] == 2
    with path.open('a') as stream:
        stream.write('\n')
    result = reports.ReportManager.get_agent_log('report-tail', 2)
    assert result['total_lines'] == 3
    assert result['logs'] == [json.loads(final)]
    assert result['status'] == 'completed'
