from radar import classificacao as classificacao_mod



def test_termo_forte_nao_transforma_aquisicao_em_servico():
    assert classificacao_mod.classificar("Aquisição de motosserras para poda de árvores") is None
    assert classificacao_mod.classificar("Aquisição de equipamentos com prestação de serviços de poda de árvores")

import pytest


@pytest.mark.parametrize(
    "objeto",
    [
        "Contratação de estudo ambiental",
        "Contratação de estudos ambientais",
        "Avaliação de impacto ambiental",
        "Avaliação de impactos ambientais",
    ],
)
def test_15_variantes_ambientais_sao_reconhecidas(objeto):
    assert classificacao_mod.classificar(objeto)


@pytest.mark.parametrize(
    "objeto",
    [
        "Estudo ambientalista sobre poesia",
        "Impacto ambientalista no debate",
        "Estudo de educação ambiental",
        "Aquisição de materiais sobre impacto ambiental",
        "Estudo do ambiente escolar",
        "Serviço de topografia e agrimensura",
    ],
)
def test_15_variantes_preservam_limites_e_exclusoes(objeto):
    assert classificacao_mod.classificar(objeto) is None


@pytest.mark.parametrize(
    "objeto",
    [
        "Estudo de impacto ambiental EIA RIMA",
        "Supressão de vegetação, inventário florestal, reposição florestal e estudo ambiental",
    ],
)
def test_15_variantes_nao_inflam_prioridade_ja_reconhecida(objeto):
    assert classificacao_mod.classificar(objeto, detalhado=True)["score"] == 9


def test_15_variante_ambiental_resgata_topografia_com_estudo():
    assert classificacao_mod.classificar("Contratação de estudo ambiental com topografia")


positivos = [
    "Contratação de serviços de Gestão Ambiental (abrangendo Supervisão Ambiental, Implementação de Programas Ambientais e Gerenciamento Ambiental) para as obras da 2ª Ponte Internacional Brasil - Uruguai sobre o Rio Jaguarão, na BR-116/RS.",
    "Contratação de Empresa especializada para Prestação de Serviços de supervisão das obras de duplicação da BR-316/PA no Estado do Pará.",
    "Registro de Preços para manejo arbóreo, compreendendo poda, destoca, remoção de árvores e transporte de resíduos.",
    "Contratação de empresa especializada para o fornecimento de serviços continuados de engenharia, sem dedicação exclusiva de mão de obra, de gestão ambiental na rodovia RSC-453/ERS-486, trecho Tainhas - Terra de Areia, incluída a interseção com a BR-101/RS, localizada nos municípios de São Francisco de Paula/RS, Itati/RS e Terra de Areia/RS, com extensão de 53,5 quilômetros.",
    "Elaboração de estudos ambientais, salvamento arqueológico e projetos básico e executivo de engenharia visando a implantação e pavimentação da Rodovia BR-070/GO.",
]
negativos = [
    "Aquisição de uniformes escolares",
    "Supervisão de obras de reforma de escola municipal",
    "Serviço de topografia para loteamento",
]


def test_classificacao_de_oportunidades_do_radar():
    for objeto in positivos:
        classificacao = classificacao_mod.classificar(objeto, detalhado=True)
        assert classificacao["manter"], f"Deveria manter: {objeto} ({classificacao})"

    for objeto in negativos:
        classificacao = classificacao_mod.classificar(objeto, detalhado=True)
        assert not classificacao["manter"], f"Deveria descartar: {objeto} ({classificacao})"


@pytest.mark.parametrize("objeto", [
    "Prestação de serviços de recapagens e vulcanizações de pneus para a Secretaria de Desenvolvimento Rural e Gestão Ambiental",
    "Serviços de corte, fresa e solda para as Secretarias de Saneamento e Gestão Ambiental e Secretaria de Obras",
    "Manutenção de veículos do Departamento Municipal de Gerenciamento Ambiental",
])
def test_nome_da_secretaria_nao_classifica_servico_alheio(objeto):
    assert classificacao_mod.classificar(objeto) is None


@pytest.mark.parametrize("objeto", [
    "Contratação de serviços de gestão ambiental para o município",
    "Secretaria de Obras contrata serviços de gestão ambiental",
    "Secretaria de Gestão Ambiental contrata serviços de gestão ambiental",
    "Secretaria de Gestão Ambiental: gerenciamento ambiental de obras",
    "Serviço de poda de árvores para a Secretaria de Gestão Ambiental",
    "Licenciamento ambiental para as Secretarias de Saneamento e Gestão Ambiental",
    "Gerenciamento ambiental para o Departamento de Gestão Ambiental",
    "Secretaria de Obras: contratação de gestão ambiental",
    "Consultoria para a Secretaria de Obras visando gestão ambiental",
])
def test_servico_ambiental_real_permanece_mesmo_com_secretaria(objeto):
    assert classificacao_mod.classificar(objeto)
