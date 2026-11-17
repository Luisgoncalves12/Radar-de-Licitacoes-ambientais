"""Exemplos da captação e limites do foco comercial das regras 5.1.7."""

from copy import deepcopy

import pytest

from radar import banco, classificacao, configuracao
from test_coleta_resiliente import registro


@pytest.mark.parametrize("objeto,termo", [
    ("Contratação de consultoria de engenharia ambiental", "consultoria de engenharia ambiental"),
    ("Serviços de consultoria em engenharia ambiental", "consultoria de engenharia ambiental"),
    ("Supervisão e consultoria ambiental das obras", "consultoria ambiental"),
    ("Consultoria em meio ambiente", "consultoria ambiental"),
    ("Serviços de assessoria ambiental", "assessoria ambiental"),
    ("Prestação de acompanhamento ambiental de empreendimentos", "acompanhamento ambiental"),
    ("Supressão vegetal da área de alague da Barragem da Arvorezinha", "supressão vegetal"),
    ("Supressão de indivíduos arbóreos devidamente autorizados", "supressão de indivíduos arbóreos"),
    ("Serviços de destocamento mecânico ou manual", "destocamento"),
    ("Obras e serviços de engenharia para implantação de ações de revitalização hidroambiental em áreas da bacia do rio São Francisco", "revitalização hidroambiental"),
    ("Projeto executivo e realização de serviços de desassoreamento do Valão Boa Vista", "desassoreamento"),
    ("Prestação de serviço de capina e roçagem nos prédios municipais", "capina"),
    ("Manutenção de canteiros centrais, plantio de grama, rega e roçagem", "manutenção de canteiros"),
    ("Supervisão da execução das obras remanescentes de duplicação e restauração da rodovia BR-101/AL", "supervisão de obras"),
    ("Elaboração de estudos e projetos básico e executivo de engenharia para implantação e pavimentação de segmentos da BR-030/BA", "projeto básico e executivo de engenharia"),
    ("Elaboração dos estudos e dos projetos básicos e executivos de engenharia, desenvolvidos de forma nativa na metodologia BIM, para duplicação da rodovia BR-158/RS", "projeto básico e executivo de engenharia"),
    ("Elaboração dos estudos e dos projetos de engenharia para duplicação da BR-158/RS", "projeto de engenharia"),
])
def test_objetos_da_captacao_selecionados_pelo_servico(objeto, termo):
    resultado = classificacao.classificar(objeto, detalhado=True)
    assert resultado["manter"], resultado
    assert termo in resultado["termos"], resultado


@pytest.mark.parametrize("orgao", [None, "DNIT", "CODEVASF"])
@pytest.mark.parametrize("objeto", [
    "Limpeza de bueiros e redes de esgoto",
    "Diagnóstico fitossanitário avançado não destrutivo",
    "Diagnóstico fitossanitário de árvores por tomografia sônica",
    "Serviços de tomografia de árvores",
    "Compra de computadores para a Secretaria de Gestão Ambiental",
    "Serviços de manutenção de veículos",
    "Aquisição de equipamentos para diagnóstico fitossanitário",
    "Aquisição de tratores para capina e roçagem",
    "Aquisição de equipamentos para supervisão e consultoria ambiental",
    "Fornecimento de sementes para revitalização hidroambiental",
    "Locação de software para consultoria ambiental",
    "Contratação de empresa para execução de pavimentação de rodovia",
])
def test_orgao_preferencial_nao_resgata_servico_alheio_ou_compra(objeto, orgao):
    assert classificacao.classificar(objeto, orgao=orgao) is None


@pytest.mark.parametrize("objeto", [
    "Supervisão da execução das obras de reforma de escola municipal",
    "Elaboração de estudos e projetos básicos e executivos de engenharia para reforma de escritório",
])
def test_variantes_de_engenharia_continuam_exigindo_contexto(objeto):
    assert classificacao.classificar(objeto) is None


def test_consultoria_com_topografia_e_contrato_misto():
    assert classificacao.classificar("Consultoria de engenharia ambiental com topografia")
    assert classificacao.classificar("Aquisição de equipamentos com prestação de serviços de supressão vegetal")


def test_edital_misto_entra_pelo_manejo_e_nao_pelo_diagnostico():
    resultado = classificacao.classificar(
        "Manejo arbóreo com poda, destocamento e diagnóstico fitossanitário por tomografia sônica",
        detalhado=True,
    )
    assert resultado["manter"]
    assert set(resultado["termos"]) == {"manejo arbóreo", "poda", "destocamento"}


def test_reclassificacao_recupera_consultoria_sem_apagar_decisao(coleta_memoria):
    config = configuracao.carregar_configuracao()
    antiga = deepcopy(config)
    for termo in ("consultoria ambiental", "consultoria de engenharia ambiental"):
        antiga["termos"].pop(termo)
    item = registro(objetoCompra="Consultoria de engenharia ambiental")
    assert classificacao.classificar(item["objetoCompra"], config=antiga) is None
    banco._salvar_descartada(coleta_memoria, item, "sem termo de relevância", config=antiga)
    banco.reclassificar_banco(coleta_memoria, config=antiga)
    with coleta_memoria:
        banco._registrar_marcacao(
            coleta_memoria, item["numeroControlePNCP"], 0, 2, 0, "teste", motivo="Prazo inviável"
        )
    banco.reclassificar_banco(coleta_memoria, config=config)
    assert coleta_memoria.execute(
        "SELECT descartada,jaCaptou,motivoDesinteresse FROM licitacoes"
    ).fetchone() == (0, 2, "Prazo inviável")
