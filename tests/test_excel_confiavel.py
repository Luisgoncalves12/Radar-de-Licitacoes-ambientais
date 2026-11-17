from radar import banco as banco_mod, classificacao as classificacao_mod, cli as cli_mod, configuracao as configuracao_mod, planilhas as planilhas_mod

import hashlib

import pandas as pd
import pytest
from openpyxl import Workbook, load_workbook



def test_4_falha_preserva_arquivo_anterior_e_remove_temporario(tmp_path, monkeypatch):
    destino = tmp_path / "relatorio.xlsx"
    livro = Workbook()
    livro.active["A1"] = "Conteúdo original"
    livro.save(destino)
    livro.close()
    antes = hashlib.sha256(destino.read_bytes()).hexdigest()
    escrever = pd.DataFrame.to_excel
    contador = []

    def falhar(self, *args, **kwargs):
        contador.append(1)
        if len(contador) == 2:
            raise RuntimeError("falha na segunda aba")
        return escrever(self, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_excel", falhar)
    with pytest.raises(RuntimeError):
        planilhas_mod._gravar_planilha(
            destino,
            [
                ("Em Aberto", pd.DataFrame({"objetoCompra": ["A"]})),
                ("Novas", pd.DataFrame({"objetoCompra": ["B"]})),
            ],
        )
    assert hashlib.sha256(destino.read_bytes()).hexdigest() == antes
    assert list(tmp_path.iterdir()) == [destino]


def test_12_texto_externo_e_gravado_como_texto_literal(tmp_path):
    destino = tmp_path / "seguro.xlsx"
    planilhas_mod._gravar_planilha(
        destino,
        [("Em Aberto", pd.DataFrame({"objetoCompra": ["=1+1", "@exemplo", "+10", "-texto"]}))],
    )
    livro = load_workbook(destino, data_only=False)
    try:
        assert livro.active["A2"].value == "=1+1"
        assert livro.active["A2"].data_type == "s"
        assert all(livro.active.cell(linha, 1).data_type == "s" for linha in range(2, 6))
    finally:
        livro.close()


def test_sem_interesse_tem_cor_condicional_e_preserva_alerta_vermelho(tmp_path):
    destino = tmp_path / "cores.xlsx"
    quadro = pd.DataFrame(
        {
            "dnit": ["DNIT", ""],
            "jaCaptou": ["Sem interesse", "Sem interesse"],
            "movimento": ["NOVA", ""],
            "situacao": ["Divulgada no PNCP", "Suspensa"],
        }
    ).rename(columns={"jaCaptou": "Já captei?"})
    planilhas_mod._gravar_planilha(destino, [("Sem interesse", quadro)])

    livro = load_workbook(destino)
    try:
        aba = livro["Sem interesse"]
        campo, = aba.conditional_formatting["B2:B3"]
        linha, = aba.conditional_formatting["A2:D3"]
        assert campo.formula == ['$B2="Sem interesse"']
        assert campo.dxf.fill.fgColor.rgb == "FFE9DDF5"
        assert 'TRIM($D2)<>"suspensa"' in linha.formula[0]
        assert 'TRIM($D2)<>"revogada"' in linha.formula[0]
        assert 'OR(COLUMN()<>1,$A2<>"DNIT")' in linha.formula[0]
        assert aba["A2"].fill.fgColor.rgb == "001F4E78"
        assert aba["C3"].fill.fgColor.rgb == "00FFC7CE"
    finally:
        livro.close()


@pytest.mark.parametrize("bloqueado", [False, True])
def test_9_exporta_abas_vazias_com_cabecalhos(coleta_memoria, monkeypatch, tmp_path, bloqueado):
    if bloqueado:
        from test_coleta_resiliente import registro

        item = registro(unidadeOrgao={"ufSigla": "SP", "municipioNome": "São José dos Campos"})
        banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", tmp_path)
    destino = tmp_path / "vazio.xlsx"
    planilhas_mod.exportar_excel(excel_path=destino)
    livro = load_workbook(destino, read_only=True)
    try:
        assert livro.sheetnames == [
            "Em Aberto",
            "DNIT",
            "Novas",
            "Atualizadas",
            "Já captei",
            "Sem interesse",
            "A abrir",
            "Acompanhamento",
        ]
        for aba in livro:
            assert aba.max_row == 1
            assert "Objeto" in [c.value for c in aba[1]]
    finally:
        livro.close()


def test_parcial_separada_identifica_prazo_invalido_e_abertura_futura(coleta_memoria, tmp_path, monkeypatch):
    from test_coleta_resiliente import registro

    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", tmp_path)
    config = configuracao_mod.configuracao_padrao()
    for item in (
        registro(1, dataAberturaProposta="2099-11-01T08:00:00"),
        registro(2, dataEncerramentoProposta="inválido"),
    ):
        banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    hoje = configuracao_mod.hoje_brasilia()
    completo = tmp_path / f"{hoje:%d.%m.%Y}.xlsx"
    completo.write_bytes(b"versao anterior")
    monkeypatch.setattr(planilhas_mod, "importar_marcacoes_captacao", lambda *a, **k: 0)
    caminho = planilhas_mod.exportar_excel(config=config, coleta={"estado": "parcial", "dia": hoje})
    assert caminho.name.endswith(".parcial.xlsx")
    assert completo.read_bytes() == b"versao anterior"
    livro = load_workbook(caminho, read_only=True)
    try:
        assert "Cobertura" not in livro.sheetnames
        assert livro["Em Aberto"].max_row == 1
        assert livro["A abrir"].max_row == 2
        assert "Revisar prazo" not in livro.sheetnames
    finally:
        livro.close()


def test_acompanhamento_mantem_edital_encerrado_e_importa_saida_da_aba(coleta_memoria, tmp_path):
    from test_coleta_resiliente import registro

    for numero in (1, 2):
        item = registro(numero, dataEncerramentoProposta="2000-01-01T12:00:00")
        banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.execute(
        "UPDATE licitacoes SET jaCaptou=1, descartada=1, dataConsultaSituacao='2026-11-10T10:00:00-03:00' "
        "WHERE numeroControlePNCP=?", (registro(1)["numeroControlePNCP"],)
    )
    coleta_memoria.commit()
    destino = tmp_path / "radar.xlsx"
    planilhas_mod.exportar_excel(excel_path=destino)

    livro = load_workbook(destino)
    try:
        aba = livro["Acompanhamento"]
        linha, = list(aba.values)[1:]
        dados = dict(zip(next(aba.values), linha))
        assert dados["Prazo das propostas"] == "Prazo encerrado"
        assert dados["Situação consultada em"].hour == 10
        assert livro["Em Aberto"].max_row == 1
        assert livro["Já captei"].max_row == 1
        aba["C2"] = "Não"
        livro.save(destino)
    finally:
        livro.close()

    planilhas_mod.exportar_excel(excel_path=destino)
    livro = load_workbook(destino, read_only=True)
    try:
        assert livro["Acompanhamento"].max_row == 1
    finally:
        livro.close()


def test_reexportacao_preserva_copia_da_planilha_corrompida(coleta_memoria, tmp_path):
    destino = tmp_path / "radar.xlsx"
    destino.write_bytes(b"arquivo danificado")

    with pytest.raises(planilhas_mod.PlanilhaIlegivel, match="Não foi possível ler"):
        planilhas_mod.exportar_excel(excel_path=destino, importar_de=destino)
    assert destino.read_bytes() == b"arquivo danificado"

    planilhas_mod.exportar_excel(excel_path=destino)
    copia, = tmp_path.glob("radar.corrompida-*.xlsx")
    assert copia.read_bytes() == b"arquivo danificado"
    livro = load_workbook(destino, read_only=True)
    try:
        assert "Em Aberto" in livro.sheetnames
    finally:
        livro.close()


def test_reexportar_parcial_preserva_edicao_antes_de_substituir(coleta_memoria, tmp_path, monkeypatch):
    from test_coleta_resiliente import registro

    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", tmp_path)
    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    hoje = configuracao_mod.hoje_brasilia()
    coleta = {"estado": "parcial", "dia": hoje}
    caminho = planilhas_mod.exportar_excel(coleta=coleta, hoje=hoje)
    livro = load_workbook(caminho)
    livro["Novas"]["C2"] = "Sim"
    livro.save(caminho)
    livro.close()

    planilhas_mod.exportar_excel(coleta=coleta, hoje=hoje)

    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone() == (1,)


def criar_planilha_legada(caminho, status):
    livro = Workbook()
    livro.active.title = "Em Aberto"
    livro.active.append(["ID", "Já captei?"])
    livro.active.append(["R-000001", status])
    livro.save(caminho)
    livro.close()


def test_planilha_legada_exige_identificacao_estavel(coleta_memoria, tmp_path):
    caminho = tmp_path / "legada.xlsx"
    criar_planilha_legada(caminho, "Não")

    with pytest.raises(ValueError, match="Número de controle PNCP ausente"):
        planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)


def preparar_relatorio(banco, tmp_path):
    from test_coleta_resiliente import registro

    item = registro()
    banco_mod.salvar_licitacao(banco, item, classificacao_mod.classificar(item["objetoCompra"]))
    banco.commit()
    caminho = tmp_path / "04.11.2026.xlsx"
    planilhas_mod.exportar_excel(excel_path=caminho)
    return caminho


def test_5_planilha_antiga_modificada_nao_reverte_banco(coleta_memoria, tmp_path, monkeypatch):
    from test_coleta_resiliente import registro
    import os

    monkeypatch.setattr(configuracao_mod, "PASTA_PLANILHAS", tmp_path)
    item = registro()
    banco_mod.salvar_licitacao(coleta_memoria, item, classificacao_mod.classificar(item["objetoCompra"]))
    coleta_memoria.execute("UPDATE licitacoes SET jaCaptou=1")
    coleta_memoria.commit()
    antiga = tmp_path / "01.11.2026.xlsx"
    criar_planilha_legada(antiga, "Não")
    os.utime(antiga, (9999999999, 9999999999))
    planilhas_mod.exportar_excel(excel_path=tmp_path / "04.11.2026.xlsx")
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone()[0] == 1


def test_8_importa_edicao_em_novas(coleta_memoria, tmp_path):
    caminho = preparar_relatorio(coleta_memoria, tmp_path)
    livro = load_workbook(caminho)
    livro["Novas"]["C2"] = "Sim"
    livro.save(caminho)
    livro.close()
    planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone()[0] == 1


def test_8_conflito_entre_abas_nao_aplica_alteracoes(coleta_memoria, tmp_path):
    caminho = preparar_relatorio(coleta_memoria, tmp_path)
    livro = load_workbook(caminho)
    livro["Em Aberto"]["C2"] = "Sim"
    livro["Novas"]["C2"] = "Sem interesse"
    livro.save(caminho)
    livro.close()
    with pytest.raises(ValueError, match="[Cc]onflito"):
        planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone()[0] == 0


def test_5_snapshot_sem_edicoes_preserva_estado_mais_recente(coleta_memoria, tmp_path):
    caminho = preparar_relatorio(coleta_memoria, tmp_path)
    coleta_memoria.execute("UPDATE licitacoes SET jaCaptou=1")
    coleta_memoria.commit()
    planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone()[0] == 1


def test_historico_registra_importacao_e_restauracao_sem_duplicar(coleta_memoria, tmp_path, monkeypatch):
    caminho = preparar_relatorio(coleta_memoria, tmp_path)
    livro = load_workbook(caminho)
    livro["Novas"]["C2"] = "Sim"
    livro.save(caminho)
    livro.close()

    assert planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho) == 1
    assert planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho) == 0
    numero, = coleta_memoria.execute("SELECT numeroControlePNCP FROM licitacoes").fetchone()
    assert coleta_memoria.execute(
        "SELECT numeroControlePNCP,valorAntigo,valorNovo,origem,reverte_id FROM historico_marcacoes"
    ).fetchall() == [(numero, 0, 1, caminho.name, None)]

    monkeypatch.setattr("sys.argv", ["radar", "--restaurar-marcacao", "1"])
    assert cli_mod.main() == 0
    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone() == (0,)
    assert cli_mod.main() == 1
    with pytest.raises(ValueError, match="decisão mais recente"):
        planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)

    saida = tmp_path / "historico" / "relatorio.xlsx"
    planilhas_mod.exportar_excel(excel_path=saida)
    livro = load_workbook(saida, read_only=True)
    try:
        assert "Histórico de marcações" not in livro.sheetnames
        assert coleta_memoria.execute(
            "SELECT id,valorAntigo,valorNovo,origem,reverte_id FROM historico_marcacoes ORDER BY id DESC"
        ).fetchall() == [(2, 1, 0, "Restauração", 1), (1, 0, 1, caminho.name, None)]
    finally:
        livro.close()


def test_falha_ao_gravar_historico_desfaz_a_marcacao(coleta_memoria, tmp_path):
    import sqlite3

    caminho = preparar_relatorio(coleta_memoria, tmp_path)
    livro = load_workbook(caminho)
    livro["Novas"]["C2"] = "Sim"
    livro.save(caminho)
    livro.close()
    coleta_memoria.execute(
        """CREATE TRIGGER falhar_historico BEFORE INSERT ON historico_marcacoes
           BEGIN SELECT RAISE(ABORT,'falha simulada'); END"""
    )

    with pytest.raises(sqlite3.IntegrityError, match="falha simulada"):
        planilhas_mod.importar_marcacoes_captacao(coleta_memoria, caminho)

    assert coleta_memoria.execute("SELECT jaCaptou FROM licitacoes").fetchone() == (0,)
    assert coleta_memoria.execute("SELECT COUNT(*) FROM historico_marcacoes").fetchone() == (0,)
