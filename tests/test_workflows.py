"""Verifica os contratos de publicacao da demonstracao publica."""

from pathlib import Path

import yaml


RAIZ = Path(__file__).resolve().parents[1]


def workflow(nome):
    return yaml.load(
        (RAIZ / '.github/workflows' / nome).read_text(encoding='utf-8'),
        Loader=yaml.BaseLoader,
    )


def test_coleta_publica_manual_sem_commit_de_resultados():
    config = workflow('automacao.yml')
    assert set(config['on']) == {'workflow_dispatch'}
    assert config['permissions'] == {'contents': 'read'}
    passos = config['jobs']['executar-script']['steps']
    comandos = '\n'.join(p.get('run', '') for p in passos)
    assert 'git push' not in comandos
    assert 'git commit' not in comandos
    validacao = next(i for i, p in enumerate(passos) if '--validar-configuracao' in p.get('run', ''))
    coleta = next(i for i, p in enumerate(passos) if p.get('id') == 'coleta')
    assert validacao < coleta
    assert 'continue-on-error' not in passos[validacao]
    assert passos[coleta]['continue-on-error'] == 'true'
    artefato = next(p for p in passos if 'upload-artifact' in p.get('uses', ''))
    assert 'always()' in artefato['if']
    assert 'radar.db' in artefato['with']['path']
    assert passos[-1]['run'] == 'exit 1'
    assert "steps.coleta.outcome == 'failure'" in passos[-1]['if']


def test_verificacao_executa_testes_validacao_e_auditoria():
    config = workflow('verificacao.yml')
    assert set(config['on']) == {'push', 'pull_request'}
    assert config['permissions'] == {'contents': 'read'}
    jobs = config['jobs']
    comandos = '\n'.join(p.get('run', '') for p in jobs['testes']['steps'])
    assert '--validar-configuracao' in comandos
    assert 'pytest' in comandos
    auditoria = '\n'.join(p.get('run', '') for p in jobs['seguranca']['steps'])
    assert 'auditar_dependencias.py requirements.txt' in auditoria
    assert 'auditar_dependencias.py requirements-dev.txt' in auditoria
