from radar import auditoria as auditoria_mod, banco as banco_mod, classificacao as classificacao_mod, cli as cli_mod, coleta as coleta_mod, configuracao as configuracao_mod, planilhas as planilhas_mod

from unittest.mock import Mock

import pandas as pd
import pytest



@pytest.mark.parametrize("pendentes,percentual,meta", [
    (18, 86.67, "Não"), (7, 94.81, "Não"), (6, 95.56, "Sim"), (0, 100, "Sim"),
])
def test_meta_de_cobertura_nao_transforma_parcial_em_completa(pendentes, percentual, meta):
    coleta = {"estado": "parcial", "dia": configuracao_mod.hoje_brasilia(),
              "total_combinacoes": 135, "pendentes": pendentes}
    resumo = auditoria_mod.resumo_coleta(coleta)
    assert resumo["Cobertura concluída (%)"] == percentual
    assert resumo["Meta de cobertura atingida"] == meta
    assert resumo["Estado"] == "parcial"


def test_cobertura_legada_desconhecida_nao_e_inventada():
    resumo = auditoria_mod.resumo_coleta({"estado": "parcial", "dia": configuracao_mod.hoje_brasilia()})
    assert resumo["Cobertura concluída (%)"] is None
    assert resumo["Meta de cobertura atingida"] == "Não disponível"


def registro(numero=1, **campos):
    item = {
        "numeroControlePNCP": f"12345678000190-1-{numero:06d}/2026",
        "objetoCompra": "Estudos ambientais para licenciamento ambiental",
        "dataEncerramentoProposta": "2099-12-15T18:00:00",
        "modalidadeId": 4,
        "unidadeOrgao": {"ufSigla": "DF", "municipioNome": "Brasília"},
        "orgaoEntidade": {"razaoSocial": "Órgão de teste"},
    }
    item.update(campos)
    return item


def test_13_mudanca_de_termos_reinicia_checkpoints(coleta_memoria, monkeypatch):
    consulta = Mock(
        return_value={"data": [registro(objetoCompra="Uniformes escolares")], "totalPaginas": 1}
    )
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    coleta_mod.coletar(delay=0)
    consulta.reset_mock()
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "termos", {"uniformes escolares": 3})
    coleta_mod.coletar(delay=0)
    assert consulta.call_count == 1
    assert coleta_memoria.execute("SELECT COUNT(*) FROM licitacoes").fetchone()[0] == 1


def test_13_assinatura_inclui_janela_e_regras(coleta_memoria, monkeypatch):
    consulta = Mock(return_value=None)
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    coleta_mod.coletar(dias_futuros=90, delay=0)
    coleta_mod.coletar(dias_futuros=30, delay=0)
    assert consulta.call_count == 2


def test_20_retoma_desde_primeira_pagina_e_recupera_item_deslocado(coleta_memoria, monkeypatch):
    def primeira(pagina, *args, **kwargs):
        if pagina == 4:
            return "FALHOU"
        return {"data": [registro(pagina)], "totalPaginas": 4}

    monkeypatch.setattr(coleta_mod, "consultar_pagina", primeira)
    coleta_mod.coletar(delay=0)
    paginas = []

    def retomada(pagina, *args, **kwargs):
        paginas.append(pagina)
        numero = {1: 5, 2: 1, 3: 2, 4: 3}[pagina]
        return {"data": [registro(numero)], "totalPaginas": 4}

    monkeypatch.setattr(coleta_mod, "consultar_pagina", retomada)
    coleta_mod.coletar(delay=0)
    assert paginas == [1, 2, 3, 4]
    assert coleta_memoria.execute("SELECT COUNT(*) FROM licitacoes").fetchone()[0] == 4
    assert coleta_memoria.execute(
        "SELECT 1 FROM licitacoes WHERE numeroControlePNCP=?", (registro(5)["numeroControlePNCP"],)
    ).fetchone() == (1,)


def test_21_coleta_fecha_banco_e_desfaz_transacao_na_excecao(coleta_memoria, monkeypatch):
    monkeypatch.setattr(
        coleta_mod, "consultar_pagina", Mock(return_value={"data": [registro()], "totalPaginas": 1})
    )

    def falhar(*args, **kwargs):
        coleta_memoria.execute(
            "INSERT INTO progresso(dia,uf,modalidade) VALUES ('2099-01-01','DF',4)"
        )
        raise RuntimeError("falha de processamento")

    monkeypatch.setattr(classificacao_mod, "classificar", falhar)
    with pytest.raises(RuntimeError):
        coleta_mod.coletar(delay=0)
    assert coleta_memoria.fechamentos == 1
    assert not coleta_memoria.in_transaction
    assert coleta_memoria.execute("SELECT COUNT(*) FROM progresso").fetchone()[0] == 0


def test_21_exportacao_fecha_banco_se_criar_pasta_falhar(coleta_memoria, monkeypatch, tmp_path):
    monkeypatch.setattr(configuracao_mod.Path, "mkdir", Mock(side_effect=PermissionError("pasta bloqueada")))
    with pytest.raises(PermissionError):
        planilhas_mod.exportar_excel(excel_path=tmp_path / "relatorio.xlsx")
    assert coleta_memoria.fechamentos == 1


def test_21_main_fecha_sessao_mesmo_se_coleta_falhar(monkeypatch):
    monkeypatch.setattr("sys.argv", ["radar"])
    sessao = Mock()
    monkeypatch.setattr(coleta_mod.requests, "Session", Mock(return_value=sessao))
    monkeypatch.setattr(coleta_mod, "coletar", Mock(side_effect=RuntimeError("erro")))
    with pytest.raises(RuntimeError):
        cli_mod.main()
    sessao.close.assert_called_once()


def test_1_coleta_parcial_nao_e_registrada_como_completa(coleta_memoria, monkeypatch):
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "ufs", ["DF", "SP"])

    def consultar(pagina, modalidade, uf, *args, **kwargs):
        return {"data": [registro()], "totalPaginas": 1} if uf == "DF" else "FALHOU"

    monkeypatch.setattr(coleta_mod, "consultar_pagina", consultar)
    resultado = coleta_mod.coletar(delay=0)
    assert resultado["estado"] == "parcial"
    assert coleta_memoria.execute("SELECT estado FROM log_execucoes").fetchone()[0] == "parcial"


def test_1_main_exporta_parcial_como_parcial_e_retorna_erro(monkeypatch):
    monkeypatch.setattr("sys.argv", ["radar"])
    resultado = {"estado": "parcial", "dia": configuracao_mod.hoje_brasilia()}
    monkeypatch.setattr(coleta_mod, "coletar", Mock(return_value=resultado))
    exportar = Mock()
    monkeypatch.setattr(planilhas_mod, "exportar_excel", exportar)
    assert cli_mod.main() == 2
    assert exportar.call_args.kwargs["coleta"] == resultado


@pytest.mark.parametrize(
    "resposta",
    [
        {"message": "erro"},
        {"data": []},
        {"data": {}, "totalPaginas": 1},
        {"data": [None], "totalPaginas": 1},
        {"data": [registro()], "totalPaginas": "3"},
        {"data": [], "totalPaginas": 3},
        {"data": [registro(unidadeOrgao=["inválido"])], "totalPaginas": 1},
        {"data": [registro(modalidadeId={})], "totalPaginas": 1},
        {"data": [registro(dataEncerramentoProposta={"dia": 25})], "totalPaginas": 1},
        {"data": [registro(orgaoEntidade={"razaoSocial": []})], "totalPaginas": 1},
    ],
)
def test_2_resposta_invalida_nao_conclui_busca(coleta_memoria, monkeypatch, resposta):
    monkeypatch.setattr(coleta_mod, "consultar_pagina", Mock(return_value=resposta))
    resultado = coleta_mod.coletar(delay=0)
    assert coleta_memoria.execute("SELECT COUNT(*) FROM progresso").fetchone()[0] == 0
    assert resultado["estado"] == "falhou"


def test_3_atualiza_e_descarta_registro_que_perdeu_relevancia(coleta_memoria, monkeypatch):
    monkeypatch.setattr(coleta_mod, "atualizar_situacoes", Mock(return_value=(0, 0)))
    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.execute("UPDATE licitacoes SET jaCaptou=1,primeiraColeta='2026-10-01'")
    coleta_memoria.commit()
    novo = registro(objetoCompra="Aquisição de uniformes escolares")
    monkeypatch.setattr(
        coleta_mod, "consultar_pagina", Mock(return_value={"data": [novo], "totalPaginas": 1})
    )
    coleta_mod.coletar(delay=0)
    linha = coleta_memoria.execute(
        "SELECT objetoCompra,descartada,jaCaptou,primeiraColeta FROM licitacoes"
    ).fetchone()
    assert linha == (novo["objetoCompra"], 1, 1, "2026-10-01")
    assert (
        coleta_memoria.execute(
            "SELECT COUNT(*) FROM historico_alteracoes WHERE campo='objetoCompra'"
        ).fetchone()[0]
        == 1
    )


def test_6_resposta_parcial_preserva_campos_e_json(coleta_memoria):
    import json

    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    parcial = registro(objetoCompra=None, orgaoEntidade={}, unidadeOrgao={}, modalidadeId=None)
    movimento = banco_mod.salvar_licitacao(
        coleta_memoria, parcial, classificacao_mod.classificar(item["objetoCompra"])
    )
    linha = coleta_memoria.execute(
        "SELECT objetoCompra,orgao,uf,modalidadeId,dadosCompletos FROM licitacoes"
    ).fetchone()
    assert linha[:4] == (item["objetoCompra"], "Órgão de teste", "DF", 4)
    assert json.loads(linha[4])["objetoCompra"] == item["objetoCompra"]
    assert movimento == "SEM ALTERACAO"


def test_10_distingue_rodadas_no_mesmo_dia(coleta_memoria, monkeypatch):
    monkeypatch.setattr(
        coleta_mod, "consultar_pagina", Mock(return_value={"data": [registro()], "totalPaginas": 1})
    )
    primeira = coleta_mod.coletar(delay=0)
    monkeypatch.setattr(coleta_mod, "consultar_pagina", Mock(return_value=None))
    segunda = coleta_mod.coletar(delay=0, nova_varredura=True)
    assert primeira["rodada_id"] != segunda["rodada_id"]
    quadro = planilhas_mod.montar_dataframe(coleta_memoria)
    assert quadro.iloc[0]["movimento"] == "NOVA"
    assert quadro.iloc[0]["cobertura"] == "FORA DA ÚLTIMA VARREDURA COMPLETA"


def test_duplicata_com_informacao_complementar_pode_se_tornar_relevante(coleta_memoria, monkeypatch):
    inicial = registro(objetoCompra="Serviços especializados")
    revisado = {
        "numeroControlePNCP": inicial["numeroControlePNCP"],
        "informacaoComplementar": "Licenciamento ambiental",
    }
    consulta = Mock(side_effect=[
        {"data": [inicial], "totalPaginas": 2},
        {"data": [revisado], "totalPaginas": 2},
    ])
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)

    assert coleta_mod.coletar(delay=0)["estado"] == "completa"
    assert coleta_memoria.execute("SELECT objetoCompra FROM licitacoes").fetchone() == (
        "Serviços especializados",
    )


@pytest.mark.parametrize("resposta", [None, {"data": [], "totalPaginas": 2}])
def test_pagina_final_inesperadamente_vazia_mantem_coleta_parcial(coleta_memoria, monkeypatch, resposta):
    consulta = Mock(side_effect=[{"data": [registro()], "totalPaginas": 2}, resposta])
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)

    resultado = coleta_mod.coletar(delay=0)
    assert resultado["estado"] == "parcial"
    assert resultado["pendentes"] == 1


def test_total_anunciado_detecta_paginas_repetidas(coleta_memoria, monkeypatch):
    resposta = {"data": [registro()], "totalPaginas": 2, "totalRegistros": 2}
    monkeypatch.setattr(coleta_mod, "consultar_pagina", Mock(return_value=resposta))

    assert coleta_mod.coletar(delay=0)["estado"] == "parcial"
    assert coleta_memoria.execute("SELECT count(*) FROM licitacoes").fetchone() == (1,)


def test_retorno_a_relevancia_e_texto_complementar_sao_atualizacoes(coleta_memoria):
    item = registro(informacaoComplementar="Escopo inicial")
    banco_mod._salvar_descartada(coleta_memoria, item, "sem termo de relevância")
    revisado = dict(item, informacaoComplementar="Escopo revisado")
    movimento = banco_mod.salvar_licitacao(
        coleta_memoria, revisado, classificacao_mod.classificar(revisado["objetoCompra"])
    )
    assert movimento == "ATUALIZADA"
    assert list(coleta_memoria.execute("SELECT campo FROM historico_alteracoes")) == [
        ("informacaoComplementar",), ("descartada",),
    ]


def test_valor_zero_explicito_remove_estimativa_antiga(coleta_memoria):
    item = registro(valorTotalEstimado=10000)
    classificacao = classificacao_mod.classificar(item["objetoCompra"])
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao)
    banco_mod.salvar_licitacao(coleta_memoria, dict(item, valorTotalEstimado=0), classificacao)

    assert coleta_memoria.execute("SELECT valorTotalEstimado FROM licitacoes").fetchone() == (0,)
    assert planilhas_mod.montar_dataframe(coleta_memoria).iloc[0]["valorTotalEstimado"] == "não informado"


def test_edital_reativado_por_reclassificacao_aparece_em_atualizadas(coleta_memoria):
    item = registro()
    banco_mod._salvar_descartada(coleta_memoria, item, "regra anterior")
    coleta_memoria.execute("UPDATE licitacoes SET primeiraColeta='2026-11-01'")

    banco_mod.reclassificar_banco(coleta_memoria)

    assert planilhas_mod.montar_dataframe(coleta_memoria).iloc[0]["movimento"] == "ATUALIZADA"
    assert coleta_memoria.execute(
        "SELECT campo,valorAntigo,valorNovo FROM historico_alteracoes"
    ).fetchone() == ("descartada", "1", "0")


def test_retomada_sem_pendencias_aplica_importacao_explicita(coleta_memoria, monkeypatch, tmp_path):
    from openpyxl import Workbook

    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.commit()
    planilha = tmp_path / "decisoes.xlsx"
    livro = Workbook()
    livro.active.title = "Em Aberto"
    livro.active.append(["numeroControlePNCP", "Já captei?"])
    livro.active.append([item["numeroControlePNCP"], "Sem interesse"])
    livro.save(planilha)
    livro.close()
    monkeypatch.setattr("sys.argv", ["radar", "--retomar", "--importar-marcacoes", str(planilha)])
    monkeypatch.setattr(
        coleta_mod, "coletar",
        Mock(return_value={"estado": "sem_pendencias", "dia": configuracao_mod.hoje_brasilia()}),
    )

    assert cli_mod.main() == 0
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone() == (2,)


def test_virada_do_dia_preserva_data_da_rodada(coleta_memoria, monkeypatch):
    relogio = Mock(side_effect=[
        pd.Timestamp("2026-11-10T23:59:59-03:00"),
        pd.Timestamp("2026-11-11T00:00:01-03:00"),
    ])
    monkeypatch.setattr(configuracao_mod, "agora_brasilia", relogio)
    monkeypatch.setattr(
        coleta_mod, "consultar_pagina", Mock(return_value={"data": [registro()], "totalPaginas": 1})
    )

    coleta_mod.coletar(delay=0)

    assert coleta_memoria.execute("SELECT primeiraColeta FROM licitacoes").fetchone() == ("2026-11-10",)
    assert coleta_memoria.execute("SELECT dia_coleta,timestamp FROM log_execucoes").fetchone() == (
        "2026-11-10", "2026-11-11T00:00:01-03:00",
    )


@pytest.mark.parametrize("data_atualizacao", ["2026-11-10T08:00:00", {}])
def test_confere_situacao_de_edital_ausente_pelo_detalhe(coleta_memoria, monkeypatch, data_atualizacao):
    agora = pd.Timestamp("2026-11-10T12:00:00-03:00")
    monkeypatch.setattr(configuracao_mod, "agora_brasilia", lambda: agora)
    item = registro(dataEncerramentoProposta="2026-11-25T18:00:00", situacaoCompraNome="Divulgada no PNCP")
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]), rodada_id="anterior")
    monkeypatch.setattr(coleta_mod, "consultar_pagina", Mock(return_value=None))
    sessao = Mock()
    resposta = sessao.get.return_value.__enter__ = Mock()
    sessao.get.return_value.__exit__ = Mock(return_value=False)
    resposta.return_value.json.return_value = dict(
        item, situacaoCompraNome="Revogada", dataAtualizacao=data_atualizacao
    )

    resultado = coleta_mod.coletar(delay=0, sessao=sessao)

    assert resultado["chamadas"] == 1
    situacao = "Revogada" if isinstance(data_atualizacao, str) else "Divulgada no PNCP"
    assert coleta_memoria.execute("SELECT situacao,ultimaRodada FROM licitacoes").fetchone() == (
        situacao, "anterior",
    )
    assert sessao.get.call_args.args[0].endswith("/orgaos/12345678000190/compras/2026/1")


def test_reexportacao_nao_abre_sessao_http(monkeypatch):
    monkeypatch.setattr("sys.argv", ["radar", "--somente-excel"])
    coleta = {"estado": "completa", "dia": configuracao_mod.hoje_brasilia()}
    monkeypatch.setattr(banco_mod, "ultima_coleta_salva", lambda: coleta)
    sessao = Mock()
    monkeypatch.setattr(coleta_mod.requests, "Session", sessao)
    exportar = Mock(return_value=None)
    monkeypatch.setattr(planilhas_mod, "exportar_excel", exportar)

    assert cli_mod.main() == 0
    sessao.assert_not_called()
    assert exportar.call_args.kwargs["coleta"] == coleta


def test_consulta_edital_acompanhado_apos_prazo_sem_reclassificar(coleta_memoria, monkeypatch):
    agora = pd.Timestamp("2026-11-10T12:00:00-03:00")
    monkeypatch.setattr(configuracao_mod, "agora_brasilia", lambda: agora)
    item = registro(
        dataEncerramentoProposta="2026-10-25T18:00:00",
        situacaoCompraNome="Divulgada no PNCP",
        dataAtualizacao="2026-10-20T08:00:00",
    )
    banco_mod._salvar_descartada(coleta_memoria, item, "regra anterior")
    coleta_memoria.execute("UPDATE licitacoes SET jaCaptou=1, dataColeta='2026-10-20'")
    coleta_memoria.commit()
    monkeypatch.setattr(banco_mod, "reclassificar_banco", Mock())
    monkeypatch.setattr(coleta_mod, "consultar_pagina", Mock(return_value=None))
    sessao = Mock()
    resposta = sessao.get.return_value.__enter__ = Mock()
    sessao.get.return_value.__exit__ = Mock(return_value=False)
    resposta.return_value.json.return_value = dict(
        item, situacaoCompraNome="Revogada", dataAtualizacao="2026-11-09T15:00:00",
        dataAtualizacaoGlobal="2026-11-09T17:00:00",
    )

    resultado = coleta_mod.coletar(delay=0, sessao=sessao)

    assert resultado["chamadas"] == 1
    assert coleta_memoria.execute(
        """SELECT situacao,descartada,dataColeta,dataConsultaSituacao,dataMovimentacaoPNCP
           FROM licitacoes"""
    ).fetchone() == ("Revogada", 1, "2026-10-20", "2026-11-10T12:00:00-03:00", "2026-11-09T17:00:00")
    assert coleta_memoria.execute("SELECT campo,valorNovo FROM historico_alteracoes").fetchone() == (
        "situacao", "Revogada",
    )


def test_resposta_sem_data_preserva_situacao_confirmada(coleta_memoria):
    item = registro(situacaoCompraNome="Revogada", dataAtualizacao="2026-11-10T12:00:00")
    classificacao = classificacao_mod.classificar(item["objetoCompra"])
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao)
    parcial = dict(item, situacaoCompraNome="Divulgada no PNCP", dataAtualizacao=None)

    assert banco_mod.salvar_licitacao(coleta_memoria, parcial, classificacao) == "SEM ALTERACAO"
    assert coleta_memoria.execute("SELECT situacao,dataAtualizacaoPNCP FROM licitacoes").fetchone() == (
        "Revogada", "2026-11-10T12:00:00",
    )


def test_execucao_apos_rodada_completa_busca_novos_registros(coleta_memoria, monkeypatch):
    consulta = Mock(
        side_effect=[
            {"data": [registro(1)], "totalPaginas": 1},
            {"data": [registro(2)], "totalPaginas": 1},
        ]
    )
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)

    primeira = coleta_mod.coletar(delay=0)
    segunda = coleta_mod.coletar(delay=0)

    assert consulta.call_count == 2
    assert segunda["estado"] == "completa"
    assert segunda["escaneados"] == 1
    assert primeira["rodada_id"] != segunda["rodada_id"]
    assert coleta_memoria.execute("SELECT COUNT(*) FROM licitacoes").fetchone()[0] == 2


def test_reexporta_rodada_interrompida_sem_usar_rotulo_da_anterior(coleta_memoria, monkeypatch, tmp_path):
    from openpyxl import load_workbook

    consulta = Mock(return_value=None)
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    completa = coleta_mod.coletar(delay=0)
    consulta.side_effect = [
        {"data": [registro()], "totalPaginas": 2},
        RuntimeError("queda após o commit da página"),
    ]

    with pytest.raises(RuntimeError, match="queda"):
        coleta_mod.coletar(delay=0)

    interrompida = banco_mod._ler_ultima_coleta(coleta_memoria)
    assert interrompida["rodada_id"] != completa["rodada_id"]
    assert interrompida["situacao_tentativa"] == "interrompida"
    assert interrompida["pendentes"] == 1
    assert coleta_memoria.execute("SELECT ultimaRodada FROM licitacoes").fetchone() == (
        interrompida["rodada_id"],
    )
    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", tmp_path)
    with pytest.raises(ValueError, match="substituída"):
        planilhas_mod.exportar_excel(coleta=completa)
    caminho = planilhas_mod.exportar_excel(coleta=interrompida)
    assert caminho.name.endswith(".parcial.xlsx")
    livro = load_workbook(caminho, read_only=True)
    try:
        assert "Cobertura" not in livro.sheetnames
        assert auditoria_mod.resumo_coleta(interrompida)["Situação da última tentativa"] == "interrompida"
        assert livro["Em Aberto"].max_row == 2
    finally:
        livro.close()


def test_retomada_incompativel_preserva_checkpoints(coleta_memoria, monkeypatch):
    monkeypatch.setitem(configuracao_mod.configuracao_padrao(), "ufs", ["DF", "SP"])
    consulta = Mock(side_effect=[None, {"data": [registro()], "totalPaginas": 2}, "FALHOU"])
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    parcial = coleta_mod.coletar(delay=0)
    feitos = coleta_memoria.execute("SELECT * FROM progresso").fetchall()
    paginas = coleta_memoria.execute("SELECT * FROM paginas_coleta").fetchall()
    consulta.reset_mock(side_effect=True)

    with pytest.raises(ValueError, match="outra configuração"):
        coleta_mod.coletar(dias_futuros=30, delay=0, somente_retomar=True)

    consulta.assert_not_called()
    assert coleta_memoria.execute("SELECT * FROM progresso").fetchall() == feitos
    assert coleta_memoria.execute("SELECT * FROM paginas_coleta").fetchall() == paginas
    consulta.return_value = None
    retomada = coleta_mod.coletar(delay=0, somente_retomar=True)
    assert retomada["rodada_id"] == parcial["rodada_id"]
    assert retomada["estado"] == "completa"
    assert consulta.call_args.args[:3] == (1, 4, "SP")


def test_rodada_sem_checkpoint_preserva_id_na_retomada(coleta_memoria, monkeypatch):
    consulta = Mock(side_effect=RuntimeError("queda antes da primeira página"))
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    with pytest.raises(RuntimeError, match="queda"):
        coleta_mod.coletar(delay=0)
    interrompida = banco_mod._ler_ultima_coleta(coleta_memoria)
    assert coleta_memoria.execute("SELECT COUNT(*) FROM progresso").fetchone() == (0,)
    consulta.side_effect = None
    consulta.return_value = None

    retomada = coleta_mod.coletar(delay=0, somente_retomar=True)

    assert retomada["rodada_id"] == interrompida["rodada_id"]
    assert retomada["estado"] == "completa"


def test_bloqueio_impede_coleta_e_exportacao_em_outra_thread(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    banco = tmp_path / "radar.db"
    destino = tmp_path / "radar.xlsx"
    with banco_mod._bloquear_banco(banco), ThreadPoolExecutor() as executor:
        coleta = executor.submit(coleta_mod.coletar, db_path=banco)
        exportacao = executor.submit(planilhas_mod.exportar_excel, banco, destino)
        for tarefa in (coleta, exportacao):
            with pytest.raises(ValueError, match="Outra execução"):
                tarefa.result(timeout=5)
        assert not banco.exists()
    assert planilhas_mod.exportar_excel(banco, destino) == destino


def test_falhas_em_ufs_iniciais_nao_bloqueiam_as_seguinte_e_ficam_para_retomar(
    coleta_memoria, monkeypatch, caplog
):
    config = configuracao_mod.configuracao_padrao()
    config["ufs"], config["modalidades"] = ["AC", "AL", "DF"], [4, 5, 6, 7, 8]

    def consultar(pagina, modalidade, uf, *args, **kwargs):
        if uf != "DF":
            return "FALHOU"
        return {"data": [registro(modalidade)], "totalPaginas": 1}

    consulta = Mock(side_effect=consultar)
    monkeypatch.setattr(coleta_mod, "consultar_pagina", consulta)
    with caplog.at_level("INFO"):
        parcial = coleta_mod.coletar(delay=0)
    assert parcial["estado"] == "parcial"
    assert parcial["pendentes"] == parcial["falhas"] == 10
    assert consulta.call_count == 15
    assert "AC | 5 modalidades pendentes" in caplog.text
    assert "DF | 0 modalidades pendentes" in caplog.text

    consulta.reset_mock(side_effect=True)
    consulta.return_value = None
    retomada = coleta_mod.coletar(delay=0, somente_retomar=True)
    assert retomada["rodada_id"] == parcial["rodada_id"]
    assert retomada["estado"] == "completa"
    assert [chamada.args[:3] for chamada in consulta.call_args_list] == [
        (1, modalidade, uf) for uf in ("AC", "AL") for modalidade in config["modalidades"]
    ]
