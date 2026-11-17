"""Prioridade comercial, prazo e decisões persistidas entre planilhas."""

import json

import pytest
from openpyxl import load_workbook

from radar import banco, classificacao, configuracao, dados, planilhas
from test_coleta_resiliente import registro


@pytest.mark.parametrize("objeto,prioridade", [
    ("Consultoria de engenharia ambiental", "1"),
    ("Supervisão e consultoria de engenharia ambiental", "1"),
    ("Supervisão ambiental das obras da BR-101", "1"),
    ("Execução de programas ambientais", "1"),
    ("Manejo arbóreo com poda e destoca", "2"),
    ("Supressão vegetal e diagnóstico fitossanitário", "2"),
    ("Diagnóstico fitossanitário por tomografia sônica", "3"),
    ("Hidrossemeadura e restauração florestal", "3"),
    ("Limpeza de bueiros", "3"),
    ("Supervisão de pavimentação com exigência de licença ambiental", "3"),
    ("Compra de computadores para a Secretaria de Gestão Ambiental", "3"),
])
def test_prioridade_depende_do_servico(objeto, prioridade):
    assert dados.prioridade_servico(objeto).startswith(prioridade)


def test_codevasf_no_orgao_ou_unidade_sem_usar_objeto():
    assert dados.orgao_preferencial("CODEVASF", "{}") == "CODEVASF"
    assert dados.orgao_preferencial(
        "Companhia de Desenvolvimento dos Vales do São Francisco e do Parnaíba", "{}"
    ) == "CODEVASF"
    assert dados.orgao_preferencial("Órgão", json.dumps({
        "unidadeOrgao": {"nomeUnidade": "1ª Superintendência Regional CODEVASF"}
    })) == "CODEVASF"
    assert dados.orgao_preferencial("Prefeitura", '{"objetoCompra":"obra CODEVASF"}') == ""


def preparar(conn, caminho):
    item = registro()
    banco.salvar_licitacao(conn, item, classificacao.classificar(item["objetoCompra"]))
    conn.commit()
    planilhas.exportar_excel(excel_path=caminho)
    return item["numeroControlePNCP"]


def editar(caminho, status, motivo, aba="Novas"):
    livro = load_workbook(caminho)
    folha = livro[aba]
    indices = {c.value: c.column for c in folha[1]}
    folha.cell(2, indices["Já captei?"], status)
    folha.cell(2, indices["Motivo do desinteresse"]).value = motivo
    livro.save(caminho)
    livro.close()


def estado(conn):
    return conn.execute("SELECT jaCaptou,motivoDesinteresse FROM licitacoes").fetchone()


def test_motivo_persiste_muda_limpa_e_restaura(coleta_memoria, tmp_path):
    conn = coleta_memoria
    caminho = tmp_path / "radar.xlsx"
    numero = preparar(conn, caminho)
    motivo = configuracao.MOTIVOS_DESINTERESSE[0]
    editar(caminho, "Sem interesse", motivo)
    assert planilhas.importar_marcacoes_captacao(conn, caminho) == 1
    assert planilhas.importar_marcacoes_captacao(conn, caminho) == 0
    assert estado(conn) == (2, motivo)
    planilhas.exportar_excel(excel_path=caminho)
    editar(caminho, "Sem interesse", "Prazo inviável", "Sem interesse")
    assert planilhas.importar_marcacoes_captacao(conn, caminho) == 1
    assert estado(conn) == (2, "Prazo inviável")
    planilhas.exportar_excel(excel_path=caminho)
    editar(caminho, "Sim", "Prazo inviável", "Sem interesse")
    assert planilhas.importar_marcacoes_captacao(conn, caminho) == 1
    assert estado(conn) == (1, "")
    assert conn.execute(
        "SELECT motivoAntigo,motivoNovo FROM historico_marcacoes ORDER BY id"
    ).fetchall() == [("", motivo), (motivo, "Prazo inviável"), ("Prazo inviável", "")]
    banco.restaurar_marcacao(conn, 3)
    assert estado(conn) == (2, "Prazo inviável")
    assert banco._estado_marcacao(conn, numero) == (2, 4)


def test_conflito_de_motivos_entre_abas_e_planilha_antiga(coleta_memoria, tmp_path):
    conn = coleta_memoria
    caminho = tmp_path / "radar.xlsx"
    numero = preparar(conn, caminho)
    editar(caminho, "Sem interesse", "Prazo inviável")
    editar(caminho, "Sem interesse", "Outro motivo", "Em Aberto")
    with pytest.raises(ValueError, match="Conflito entre abas"):
        planilhas.importar_marcacoes_captacao(conn, caminho)
    assert estado(conn) == (0, "")
    editar(caminho, "Sem interesse", "Prazo inviável", "Em Aberto")
    planilhas.importar_marcacoes_captacao(conn, caminho)
    planilhas.exportar_excel(excel_path=caminho)
    with conn:
        banco._registrar_marcacao(conn, numero, 2, 2, 1, "outra decisão", motivo="Outro motivo")
    # Snapshot intacto não desfaz a decisão mais recente.
    assert planilhas.importar_marcacoes_captacao(conn, caminho) == 0
    editar(caminho, "Sem interesse", "Preço não competitivo", "Sem interesse")
    with pytest.raises(ValueError, match="decisão mais recente"):
        planilhas.importar_marcacoes_captacao(conn, caminho)
    assert estado(conn) == (2, "Outro motivo")


def test_prazo_precede_prioridade_e_orgao(coleta_memoria, tmp_path):
    conn = coleta_memoria
    for item in (
        registro(1, objetoCompra="Hidrossemeadura", dataEncerramentoProposta="2099-12-15T08:00:00"),
        registro(2, objetoCompra="Supervisão ambiental", orgaoEntidade={"razaoSocial": "DNIT"},
                 dataEncerramentoProposta="2099-12-15T17:00:00"),
        registro(3, orgaoEntidade={"razaoSocial": "CODEVASF"}, dataEncerramentoProposta="2099-12-16T08:00:00"),
    ):
        banco.salvar_licitacao(conn, item, (3, ["ambiental"], "ambiental"))
    conn.commit()
    df = planilhas.montar_dataframe(conn)
    assert df["idVisivel"].tolist() == ["R-000001", "R-000002", "R-000003"]
    assert df["dnit"].tolist() == ["", "DNIT", "CODEVASF"]


def test_lista_lembrete_e_motivo_vazio_nao_bloqueiam(coleta_memoria, tmp_path):
    caminho = tmp_path / "radar.xlsx"
    preparar(coleta_memoria, caminho)
    livro = load_workbook(caminho)
    folha = livro["Novas"]
    for aba in livro:
        cabecalhos = [c.value for c in aba[1]]
        assert "Prioridade de servico" not in cabecalhos
        if "Cobertura da coleta" in cabecalhos:
            assert cabecalhos.index("Motivo do desinteresse") == cabecalhos.index("Cobertura da coleta") + 1
        else:
            assert cabecalhos[-1] == "Motivo do desinteresse"
    validacao = next(v for v in folha.data_validations.dataValidation if "Falta de atestado" in v.formula1)
    assert len(validacao.formula1) <= 255
    assert validacao.allow_blank
    assert any('="Sem interesse"' in regra.formula[0] and '=""' in regra.formula[0]
               for faixa in folha.conditional_formatting for regra in folha.conditional_formatting[faixa])
    assert "CODEVASF" not in livro.sheetnames
    livro.close()
    editar(caminho, "Sem interesse", None)
    assert planilhas.importar_marcacoes_captacao(coleta_memoria, caminho) == 1
    assert estado(coleta_memoria) == (2, "")


def test_planilha_v2_sem_coluna_preserva_motivo(coleta_memoria, tmp_path):
    from openpyxl.packaging.custom import StringProperty

    caminho = tmp_path / "radar.xlsx"
    numero = preparar(coleta_memoria, caminho)
    with coleta_memoria:
        banco._registrar_marcacao(coleta_memoria, numero, 0, 2, 0, "teste", motivo="Prazo inviável")
    planilhas.exportar_excel(excel_path=caminho)
    livro = load_workbook(caminho)
    referencias = json.loads(livro.custom_doc_props["RadarMarcacoes"].value)
    referencias["versao"] = 2
    for ref in referencias["linhas"].values():
        del ref["motivo"]
    del livro.custom_doc_props["RadarMarcacoes"]
    livro.custom_doc_props.append(StringProperty(name="RadarMarcacoes", value=json.dumps(referencias)))
    for folha in livro:
        for celula in folha[1]:
            if celula.value == "Motivo do desinteresse":
                folha.delete_cols(celula.column)
                break
    livro.save(caminho)
    livro.close()
    assert planilhas.importar_marcacoes_captacao(coleta_memoria, caminho) == 0
    assert estado(coleta_memoria) == (2, "Prazo inviável")


def test_snapshot_v2_intacto_nao_propoe_limpeza_de_motivo_novo(coleta_memoria, tmp_path):
    caminho = tmp_path / "radar.xlsx"
    numero = preparar(coleta_memoria, caminho)
    livro = load_workbook(caminho)
    referencias = json.loads(livro.custom_doc_props["RadarMarcacoes"].value)
    # Simula metadados de versão antiga, sem referência de motivo.
    from openpyxl.packaging.custom import StringProperty
    referencias["versao"] = 2
    for ref in referencias["linhas"].values():
        del ref["motivo"]
    del livro.custom_doc_props["RadarMarcacoes"]
    livro.custom_doc_props.append(StringProperty(name="RadarMarcacoes", value=json.dumps(referencias)))
    livro.save(caminho)
    livro.close()
    with coleta_memoria:
        banco._registrar_marcacao(coleta_memoria, numero, 0, 2, 0, "nova decisão", motivo="Prazo inviável")
    assert planilhas.importar_marcacoes_captacao(coleta_memoria, caminho) == 0
    assert estado(coleta_memoria) == (2, "Prazo inviável")


def test_motivo_invalido_nao_aplica_decisao(coleta_memoria, tmp_path):
    caminho = tmp_path / "radar.xlsx"
    preparar(coleta_memoria, caminho)
    editar(caminho, "Sem interesse", "texto fora da lista")
    with pytest.raises(ValueError, match="Motivo do desinteresse inválido"):
        planilhas.importar_marcacoes_captacao(coleta_memoria, caminho)
    assert estado(coleta_memoria) == (0, "")


def test_migracao_preserva_banco_antigo_e_historico(tmp_path):
    import sqlite3
    caminho = tmp_path / "antigo.db"
    conn = sqlite3.connect(caminho)
    conn.execute("""CREATE TABLE historico_marcacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, numeroControlePNCP TEXT NOT NULL,
        data TEXT NOT NULL, valorAntigo INTEGER NOT NULL, valorNovo INTEGER NOT NULL,
        origem TEXT NOT NULL, reverte_id INTEGER)""")
    conn.execute("INSERT INTO historico_marcacoes VALUES (1,'edital','2026-11-16',0,2,'antiga',NULL)")
    conn.commit()
    conn.close()
    conn = banco.iniciar_banco(caminho)
    try:
        assert conn.execute(
            "SELECT valorNovo,motivoAntigo,motivoNovo FROM historico_marcacoes"
        ).fetchall() == [(2, "", "")]
        assert "motivoDesinteresse" in {r[1] for r in conn.execute("PRAGMA table_info(licitacoes)")}
    finally:
        conn.close()
