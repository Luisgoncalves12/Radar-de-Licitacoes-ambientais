from radar import banco as banco_mod, classificacao as classificacao_mod, configuracao as configuracao_mod, dados as dados_mod, planilhas as planilhas_mod

from datetime import date, timedelta

from openpyxl import load_workbook



def item(numero="12345678000190-1-000001/2026", **changes):
    dado = {
        "numeroControlePNCP": numero,
        "objetoCompra": "Estudos ambientais para licenciamento ambiental",
        "dataAberturaProposta": "2026-10-06T08:00:00",
        "dataEncerramentoProposta": "2026-10-20T10:00:00",
        "dataAtualizacao": "2026-10-01T09:00:00",
        "situacaoCompraId": 1,
        "situacaoCompraNome": "Divulgada",
        "valorTotalEstimado": 1000,
        "linkSistemaOrigem": "https://example.test/edital",
        "modalidadeId": 4,
        "unidadeOrgao": {"ufSigla": "DF", "municipioNome": "Brasília"},
        "orgaoEntidade": {"razaoSocial": "Órgão de teste"},
    }
    dado.update(changes)
    return dado


def classificar(item_data):
    resultado = classificacao_mod.classificar(item_data["objetoCompra"])
    assert resultado is not None
    return resultado


def test_salvar_mesmo_item_duas_vezes_nao_duplica(tmp_path):
    conn = banco_mod.iniciar_banco(tmp_path / "teste.db")
    registro = item()
    banco_mod.salvar_licitacao(conn, registro, classificar(registro))
    banco_mod.salvar_licitacao(conn, registro, classificar(registro))
    assert conn.execute("SELECT COUNT(*) FROM licitacoes").fetchone()[0] == 1
    conn.close()


def test_atualiza_status_e_prazo_preservando_primeira_coleta_e_historico(tmp_path):
    conn = banco_mod.iniciar_banco(tmp_path / "teste.db")
    registro = item()
    banco_mod.salvar_licitacao(conn, registro, classificar(registro))
    conn.execute("UPDATE licitacoes SET primeiraColeta='2026-09-01'")
    atualizado = item(situacaoCompraNome="Suspensa", dataEncerramentoProposta="2026-10-25T10:00:00")
    banco_mod.salvar_licitacao(conn, atualizado, classificar(atualizado))
    linha = conn.execute(
        "SELECT primeiraColeta,situacao,dataEncerramentoProposta FROM licitacoes"
    ).fetchone()
    assert linha == ("2026-09-01", "Suspensa", "2026-10-25T10:00:00")
    historico = conn.execute("SELECT campo FROM historico_alteracoes").fetchall()
    assert {r[0] for r in historico} >= {"situacao", "dataEncerramentoProposta"}
    conn.close()


def test_topografia_isolada_e_descartada():
    resultado = classificacao_mod.classificar(
        "Contratação de serviços de topografia e agrimensura", detalhado=True
    )
    assert not resultado["manter"]
    assert resultado["motivo"] == "topografia/agrimensura sem serviço-alvo"
    plural = classificacao_mod.classificar("Contratação de agrimensores para serviços de campo", detalhado=True)
    assert not plural["manter"]
    assert plural["motivo"] == "topografia/agrimensura sem serviço-alvo"


def test_estudo_ambiental_com_topografia_e_mantido():
    assert classificacao_mod.classificar(
        "Elaboração de estudos ambientais para licenciamento, incluindo levantamento topográfico"
    )


def test_topografia_com_apenas_termos_fracos_e_descartada():
    resultado = classificacao_mod.classificar(
        "Projeto executivo de engenharia com topografia e sondagem", detalhado=True
    )
    assert not resultado["manter"]
    assert resultado["motivo"] == "topografia/agrimensura sem serviço-alvo"


def test_termos_fortes_passam_e_compra_de_bens_continua_excluida():
    assert classificacao_mod.classificar("Monitoramento ambiental de área de preservação")
    assert (
        classificacao_mod.classificar("Aquisição de equipamentos para projeto executivo de engenharia") is None
    )


def test_quatro_objetos_enviados_pelo_dono():
    casos = [
        (
            "Apoio ao PROJETO Gestão AMBIENTAL e Territorial Autônoma de Terras de Quilombos...",
            2,
            ["gestão ambiental"],
            True,
        ),
        (
            "Registro de Preço para Futura e Eventual Contratação de Empresa Especializada para a Prestação de Serviços de HidrosSEMEADURA, com Fornecimento de Mão de Obra...",
            3,
            ["hidrossemeadura"],
            True,
        ),
        (
            "RESTAURACAO FLORESTAL de áreas DEGRADADAs na Unidade de Conservação APA Triunfo do Xingu, e Parque Estadual do Charapucu...",
            5,
            ["restauração florestal", "unidade de conservação"],
            True,
        ),
        (
            "CONTRATAÇÃO DE EMPRESA ESPECIALIZADA EM PRESTAÇÃO DE SERVIÇOS DE MANEJO ARBOREO URBANO (PODA EM ALTURA, CORTE RASO/SUPRESSÃO E DESTOCAMENTO MECÂNICO OU MANUAL) E DIAGNÓSTICO FITOSSANITÁRIO AVANÇADO NÃO DESTRUTIVO POR TOMOGRAFIA SÔNICA DE IMPULSO...",
            12,
            ["manejo arbóreo", "poda", "destocamento", "corte raso"],
            True,
        ),
    ]
    for objeto, score, termos, manter in casos:
        resultado = classificacao_mod.classificar(objeto, detalhado=True)
        assert resultado["score"] == score
        assert resultado["termos"] == termos
        assert resultado["manter"] is manter


def test_novos_termos_e_projeto_de_engenharia_exigem_contexto_para_termo_generico():
    for termo in (
        "hidrossemeadura",
        "revegetação",
        "plantio de mudas",
        "recomposição vegetal",
        "espeleologia",
        "resgate de fauna",
    ):
        resultado = classificacao_mod.classificar(f"Contratação de serviço de {termo}", detalhado=True)
        assert resultado["manter"], (termo, resultado)
    sem_contexto = classificacao_mod.classificar("Elaboração de projeto de engenharia", detalhado=True)
    com_dnit = classificacao_mod.classificar(
        "Elaboração de projeto de engenharia", detalhado=True, orgao="DNIT"
    )
    assert not sem_contexto["manter"]
    assert "projeto de engenharia" in com_dnit["termos"]
    assert "patrimônio cultural" not in configuracao_mod.configuracao_padrao()["termos"]


def test_data_atualizacao_pncp_sozinha_nao_marca_licitacao_atualizada(tmp_path):
    conn = banco_mod.iniciar_banco(tmp_path / "timestamp.db")
    registro = item()
    banco_mod.salvar_licitacao(conn, registro, classificar(registro))
    conn.commit()
    atualizado = item(dataAtualizacao="2026-10-07T10:00:00")
    movimento = banco_mod.salvar_licitacao(conn, atualizado, classificar(atualizado))
    assert movimento == "SEM ALTERACAO"
    assert (
        conn.execute("SELECT dataAtualizacaoPNCP FROM licitacoes").fetchone()[0]
        == "2026-10-07T10:00:00"
    )
    assert conn.execute("SELECT COUNT(*) FROM historico_alteracoes").fetchone()[0] == 0
    conn.close()


def test_exporta_abas_apenas_com_prazo_minimo_e_sem_descartadas(tmp_path):
    banco = tmp_path / "teste.db"
    saida = tmp_path / "resultado.xlsx"
    conn = banco_mod.iniciar_banco(banco)
    hoje = date.today()
    antigo = item("12345678000190-1-000001/2026")
    antigo["dataEncerramentoProposta"] = (hoje + timedelta(days=10)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, antigo, classificar(antigo))
    conn.execute(
        "UPDATE licitacoes SET primeiraColeta=? WHERE numeroControlePNCP=?",
        ((hoje - timedelta(days=7)).isoformat(), antigo["numeroControlePNCP"]),
    )
    nova = item("12345678000190-1-000002/2026")
    nova["dataEncerramentoProposta"] = (hoje + timedelta(days=12)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, nova, classificar(nova))
    atualizada = item("12345678000190-1-000004/2026")
    atualizada["dataEncerramentoProposta"] = (hoje + timedelta(days=14)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, atualizada, classificar(atualizada))
    conn.execute(
        "UPDATE licitacoes SET primeiraColeta=? WHERE numeroControlePNCP=?",
        ((hoje - timedelta(days=3)).isoformat(), atualizada["numeroControlePNCP"]),
    )
    atualizada["situacaoCompraNome"] = "Suspensa"
    banco_mod.salvar_licitacao(conn, atualizada, classificar(atualizada))
    descartada = item(
        "12345678000190-1-000003/2026", objetoCompra="Contratação de topografia e agrimensura"
    )
    banco_mod._salvar_descartada(conn, descartada, "topografia/agrimensura sem serviço-alvo")
    banco_mod.registrar_execucao(conn, 0, 0, 0, False)
    conn.commit()
    conn.close()

    planilhas_mod.exportar_excel(banco, saida)
    workbook = load_workbook(saida, read_only=True, data_only=True)
    assert workbook.sheetnames == [
        "Em Aberto",
        "DNIT",
        "Novas",
        "Atualizadas",
        "Já captei",
        "Sem interesse",
        "A abrir",
        "Acompanhamento",
    ]
    abertos = workbook["Em Aberto"]
    cabecalhos = [celula.value for celula in next(abertos.iter_rows(min_row=1, max_row=1))]
    assert abertos["A1"].alignment.horizontal == "center"
    assert abertos["A1"].alignment.vertical == "center"
    assert abertos["A1"].border.left.style == "thin"
    assert cabecalhos == [
        "Prioridade",
        "ID",
        "Já captei?",
        "Atualização",
        "Dias restantes",
        "Fim das propostas",
        "Objeto",
        "Órgão",
        "UF",
        "Município",
        "Tipo de publicação",
        "Valor estimado",
        "Modalidade",
        "Fonte orçamentária",
        "Score",
        "Situação",
        "Início das propostas",
        "Termos encontrados",
        "Primeira identificação",
        "Última coleta",
        "Abrir edital",
        "Cobertura da coleta",
        "Motivo do desinteresse",
    ]
    assert all(
        linha[cabecalhos.index("Dias restantes")] >= configuracao_mod.DIAS_MINIMOS
        for linha in abertos.iter_rows(min_row=2, values_only=True)
    )
    assert [
        linha[cabecalhos.index("Dias restantes")]
        for linha in abertos.iter_rows(min_row=2, values_only=True)
    ] == sorted(
        linha[cabecalhos.index("Dias restantes")]
        for linha in abertos.iter_rows(min_row=2, values_only=True)
    )
    ids = {
        linha[cabecalhos.index("Objeto")]
        for linha in abertos.iter_rows(min_row=2, values_only=True)
    }
    assert antigo["objetoCompra"] in ids
    assert nova["objetoCompra"] in ids
    assert workbook["Atualizadas"].max_row == 2
    workbook.close()


def test_anulada_com_prazo_curto_entra_em_vermelho(tmp_path):
    banco = tmp_path / "teste.db"
    saida = tmp_path / "resultado.xlsx"
    conn = banco_mod.iniciar_banco(banco)
    hoje = date.today()
    anulada = item("12345678000190-1-000011/2026", situacaoCompraNome="Anulada")
    anulada["dataEncerramentoProposta"] = (hoje + timedelta(days=2)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, anulada, classificar(anulada))
    revogada = item("12345678000190-1-000012/2026", situacaoCompraNome="Revogada")
    revogada["dataEncerramentoProposta"] = (hoje + timedelta(days=1)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, revogada, classificar(revogada))
    curta = item(
        "12345678000190-1-000013/2026",
        objetoCompra="Estudos ambientais para licenciamento ambiental item de controle",
    )
    curta["dataEncerramentoProposta"] = (hoje + timedelta(days=3)).isoformat() + "T10:00:00"
    banco_mod.salvar_licitacao(conn, curta, classificar(curta))
    conn.commit()
    conn.close()

    df = planilhas_mod.montar_dataframe(conn=banco_mod.iniciar_banco(banco), hoje=hoje)
    canceladas = df[df["situacao"].map(dados_mod._situacao_cancelada)]
    assert len(canceladas) == 2
    assert set(canceladas["objetoCompra"]) == {anulada["objetoCompra"], revogada["objetoCompra"]}
    assert all(canceladas["dias_restantes"] < configuracao_mod.DIAS_MINIMOS)
    assert curta["objetoCompra"] not in set(df["objetoCompra"])
    planilhas_mod._gravar_planilha(saida, [("Em Aberto", df)])
    aba = load_workbook(saida, data_only=True)["Em Aberto"]
    colunas = [c.value for c in aba[1]]
    sit_idx = colunas.index("Situação") + 1
    for linha in range(2, aba.max_row + 1):
        if dados_mod._situacao_cancelada(aba.cell(linha, sit_idx).value):
            assert aba.cell(linha, 1).fill.fgColor.rgb == "00FFC7CE"
            assert aba.cell(linha, 1).font.color.rgb == "009C0006"
    aba.parent.close()
