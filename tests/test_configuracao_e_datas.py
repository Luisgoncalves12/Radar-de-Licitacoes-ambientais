from radar import banco as banco_mod, classificacao as classificacao_mod, coleta as coleta_mod, configuracao as configuracao_mod, planilhas as planilhas_mod

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

import radar_pncp_v5 as radar


def gravar_configuracao(tmp_path, config):
    caminho = tmp_path / "configuracao.json"
    caminho.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return caminho


def test_configuracao_atual_e_valida():
    assert configuracao_mod.carregar_configuracao() == configuracao_mod.configuracao_padrao()


@pytest.mark.parametrize(
    ("campo", "valor", "mensagem"),
    [
        ("termos", {}, "termos e pesos"),
        ("termos", [], "termos e pesos"),
        ("termos", {"   ": 3}, "termo/peso inválido"),
        ("termos", {"ambiental": True}, "termo/peso inválido"),
        ("termos", {"ambiental": "3"}, "termo/peso inválido"),
        ("termos", {"ambiental": 4}, "termo/peso inválido"),
        ("score_minimo", 0, "score_minimo"),
        ("score_minimo", True, "score_minimo"),
        ("ufs", [], "ufs"),
        ("ufs", ["XX"], "ufs"),
        ("modalidades", [], "modalidades"),
        ("modalidades", [True], "modalidades"),
        ("modalidades", [99], "nomes_modalidades"),
        ("exclusoes", [], "exclusoes"),
        ("exclusoes", ["["], "expressão regular inválida em exclusoes"),
        ("resgate", ["("], "expressão regular inválida em resgate"),
        ("palavras_curtas", "eia", "palavras_curtas"),
        ("nomes_modalidades", [], "nomes_modalidades"),
        ("rotulos_captacao", {"nao": "Não"}, "rotulos_captacao"),
        ("status_captacao", {}, "status_captacao"),
    ],
)
def test_rejeita_configuracao_invalida(tmp_path, campo, valor, mensagem):
    config = {**configuracao_mod.configuracao_padrao(), campo: valor}
    with pytest.raises(ValueError, match=mensagem):
        configuracao_mod.carregar_configuracao(gravar_configuracao(tmp_path, config))


@pytest.mark.parametrize(
    "conteudo,mensagem",
    [
        ('{"termos":', "JSON inválido na linha 1"),
        ("[]", "raiz do JSON"),
        ("{}", "campos obrigatórios ausentes: termos"),
    ],
)
def test_rejeita_json_invalido_ou_incompleto(tmp_path, conteudo, mensagem):
    caminho = tmp_path / "configuracao.json"
    caminho.write_text(conteudo, encoding="utf-8")
    with pytest.raises(ValueError, match=mensagem):
        configuracao_mod.carregar_configuracao(caminho)


def test_arquivo_ausente_tem_mensagem_clara(tmp_path):
    with pytest.raises(ValueError, match="não foi possível ler a configuração"):
        configuracao_mod.carregar_configuracao(tmp_path / "inexistente.json")


def test_configuracao_invalida_falha_antes_de_criar_banco_ou_planilha(tmp_path):
    script = tmp_path / "radar_pncp_v5.1.py"
    shutil.copyfile(radar.__file__, script)
    shutil.copytree(Path(radar.__file__).parent / "radar", tmp_path / "radar")
    gravar_configuracao(tmp_path, {**configuracao_mod.configuracao_padrao(), "termos": {}})
    resultado = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
    )
    assert resultado.returncode != 0
    assert "Erro de configuração:" in resultado.stderr
    assert "informe termos e pesos" in resultado.stderr
    assert "Traceback" not in resultado.stderr
    assert not (tmp_path / "radar.db").exists()
    assert not (tmp_path / "planilhas").exists()


def test_relogio_converte_utc_para_brasilia(monkeypatch):
    instante = pd.Timestamp("2026-11-04T01:06:25Z")

    class TimestampFixo(pd.Timestamp):
        @classmethod
        def now(cls, tz=None):
            return instante.tz_convert(tz)

    monkeypatch.setattr(configuracao_mod.pd, "Timestamp", TimestampFixo)
    assert configuracao_mod.hoje_brasilia() == date(2026, 11, 3)
    assert configuracao_mod.agora_brasilia().isoformat() == "2026-11-03T22:06:25-03:00"


def test_coleta_e_exportacao_usam_o_dia_de_brasilia_apos_meia_noite_utc(tmp_path, monkeypatch):
    instante = pd.Timestamp("2026-11-04T01:06:25Z").tz_convert("America/Sao_Paulo")
    monkeypatch.setattr(configuracao_mod, "agora_brasilia", lambda: instante)
    banco = tmp_path / "radar.db"
    planilhas = tmp_path / "planilhas"
    monkeypatch.setattr(configuracao_mod, "DB_PATH", banco)
    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", planilhas)
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "ufs", ["DF"])
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "modalidades", [4])
    registro = {
        "numeroControlePNCP": "12345678000190-1-000001/2026",
        "objetoCompra": "Estudos ambientais para licenciamento ambiental",
        "dataEncerramentoProposta": "2026-12-01T18:00:00",
        "modalidadeId": 4,
        "unidadeOrgao": {"ufSigla": "DF", "municipioNome": "Brasília"},
    }
    consultas = []

    def consultar(pagina, modalidade, uf, inicio, fim, **kwargs):
        consultas.append(inicio)
        return {"data": [registro], "totalPaginas": 1}

    monkeypatch.setattr(coleta_mod, "consultar_pagina", consultar)
    coleta_mod.coletar(delay=0)
    planilhas_mod.exportar_excel()
    assert consultas == ["20261103"]
    with banco_mod.iniciar_banco(banco) as conn:
        assert conn.execute("SELECT dia FROM progresso").fetchone() == ("2026-11-03",)
        assert conn.execute("SELECT dataColeta,primeiraColeta FROM licitacoes").fetchone() == (
            "2026-11-03",
            "2026-11-03",
        )
        assert conn.execute("SELECT timestamp FROM log_execucoes").fetchone() == (
            "2026-11-03T22:06:25-03:00",
        )
        atualizado = {**registro, "dataEncerramentoProposta": "2026-12-02T18:00:00"}
        banco_mod.salvar_licitacao(conn, atualizado, classificacao_mod.classificar(atualizado["objetoCompra"]))
        resumo = conn.execute("SELECT resumoAlteracoes FROM licitacoes").fetchone()[0]
        assert resumo.startswith("03/11/2026:")
    assert not (planilhas / "04.11.2026.xlsx").exists()
    with (planilhas / "03.11.2026.xlsx").open("rb") as arquivo:
        livro = load_workbook(arquivo, read_only=True)
        assert livro["Novas"].max_row == 2
        livro.close()
