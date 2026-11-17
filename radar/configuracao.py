"""Configuracao, caminhos e relogio do Radar."""

from functools import lru_cache
from pathlib import Path
import json
import logging
import re

import pandas as pd


URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
URL_DETALHE = "https://pncp.gov.br/api/consulta/v1/orgaos/{cnpj}/compras/{ano}/{sequencial}"
BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "radar.db"
PASTA_PLANILHAS = BASE_DIR / "planilhas"
DIAS_MINIMOS = 7
TAMANHO_PAGINA = 50
LOCAIS_BLOQUEADOS = {("SP", "sao jose dos campos")}
FUSO_HORARIO = "America/Sao_Paulo"
VERSAO_REGRAS = "5.1.7"
MOTIVOS_DESINTERESSE = (
    "Falta de atestado / acervo técnico",
    "Falta de parceiro disponível",
    "Equipe indisponível",
    "Equipe sem habilitação / pontuação",
    "Preço não competitivo",
    "Prazo inviável",
    "Fora do escopo de atuação",
    "Impedimento comercial / estratégico",
    "Outro motivo",
)
PRAZO_TOTAL_PADRAO = 5400
VARIANTES_TERMOS = {
    "estudos ambientais": (
        "estudo ambiental", "estudos técnicos e ambientais",
        "estudos geotécnicos e ambientais",
        "estudos geotécnicos, topográficos, de tráfego e ambientais",
    ),
    "impactos ambientais": ("impacto ambiental",),
    "consultoria ambiental": ("consultoria em meio ambiente", "consultoria na área ambiental"),
    "consultoria de engenharia ambiental": ("consultoria em engenharia ambiental",),
    "supervisão de obras": (
        "supervisão da execução das obras", "supervisão da execução de obras",
        "supervisão das obras remanescentes", "supervisão de obras remanescentes",
    ),
    "estudos e projetos de engenharia": (
        "estudos e projetos básico e executivo de engenharia",
        "estudos e projetos básicos e executivos de engenharia",
        "estudos e dos projetos básicos e executivos de engenharia",
        "estudos e dos projetos de engenharia",
    ),
}


def agora_brasilia():
    return pd.Timestamp.now(tz=FUSO_HORARIO)


def hoje_brasilia():
    return agora_brasilia().date()


def carregar_configuracao(caminho=None):
    """Lê e valida os parâmetros de busca e classificação."""
    caminho = Path(caminho) if caminho is not None else BASE_DIR / "configuracao.json"
    try:
        with caminho.open(encoding="utf-8") as arquivo:
            config = json.load(arquivo)
    except json.JSONDecodeError as erro:
        raise ValueError(
            f"{caminho}: JSON inválido na linha {erro.lineno}, coluna {erro.colno}: {erro.msg}"
        ) from erro
    except (OSError, UnicodeError) as erro:
        raise ValueError(f"{caminho}: não foi possível ler a configuração: {erro}") from erro
    if not isinstance(config, dict):
        raise ValueError(f"{caminho}: a raiz do JSON deve ser um objeto")
    obrigatorios = (
        "termos",
        "palavras_curtas",
        "exclusoes",
        "score_minimo",
        "status_captacao",
        "rotulos_captacao",
        "topografia_familia",
        "sugestoes_novos_termos",
        "modalidades",
        "nomes_modalidades",
        "ufs",
        "resgate",
    )
    faltantes = [chave for chave in obrigatorios if chave not in config]
    if faltantes:
        raise ValueError(f"{caminho}: campos obrigatórios ausentes: {', '.join(faltantes)}")
    termos = config["termos"]
    if not isinstance(termos, dict) or not termos:
        raise ValueError(f"{caminho}: informe termos e pesos")
    for termo, peso in termos.items():
        if (
            not isinstance(termo, str)
            or not termo.strip()
            or type(peso) is not int
            or peso not in (1, 2, 3)
        ):
            raise ValueError(f"{caminho}: termo/peso inválido: {termo!r} = {peso!r}")
    for chave in (
        "palavras_curtas",
        "exclusoes",
        "topografia_familia",
        "sugestoes_novos_termos",
        "ufs",
        "resgate",
    ):
        valores = config[chave]
        if (
            not isinstance(valores, list)
            or any(not isinstance(v, str) or not v.strip() for v in valores)
            or (chave in ("exclusoes", "resgate", "ufs") and not valores)
        ):
            raise ValueError(
                f"{caminho}: {chave} deve ser uma lista de textos válidos"
                " (exclusoes, resgate e ufs não podem estar vazios)"
            )
    for chave in ("exclusoes", "resgate"):
        try:
            re.compile("|".join(config[chave]), re.IGNORECASE)
        except re.error as erro:
            raise ValueError(f"{caminho}: expressão regular inválida em {chave}: {erro}") from erro
    score = config["score_minimo"]
    if type(score) is not int or score <= 0:
        raise ValueError(f"{caminho}: score_minimo deve ser um inteiro positivo")
    modalidades = config["modalidades"]
    if (
        not isinstance(modalidades, list)
        or not modalidades
        or any(type(v) is not int or v <= 0 for v in modalidades)
    ):
        raise ValueError(f"{caminho}: modalidades deve conter códigos inteiros positivos")
    ufs_validas = set(
        "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
    )
    if not set(config["ufs"]) <= ufs_validas:
        raise ValueError(f"{caminho}: ufs contém siglas inválidas")
    for chave in ("rotulos_captacao", "nomes_modalidades"):
        valores = config[chave]
        if (
            not isinstance(valores, dict)
            or not valores
            or any(
                not k.isdecimal() or not isinstance(v, str) or not v.strip()
                for k, v in valores.items()
            )
        ):
            raise ValueError(f"{caminho}: {chave} deve mapear códigos numéricos para textos")
    if not {str(v) for v in modalidades} <= config["nomes_modalidades"].keys():
        raise ValueError(
            f"{caminho}: nomes_modalidades deve nomear todas as modalidades configuradas"
        )
    if config["status_captacao"] != {"nao": 0, "sim": 1, "sem interesse": 2}:
        raise ValueError(
            f"{caminho}: status_captacao deve manter os códigos nao=0, sim=1 e sem interesse=2"
        )
    if config["rotulos_captacao"] != {"0": "Não", "1": "Sim", "2": "Sem interesse"}:
        raise ValueError(
            f"{caminho}: rotulos_captacao deve manter os rótulos Não, Sim e Sem interesse para 0, 1 e 2"
        )
    return config


DELAY_ENTRE_REQUISICOES = 1.0
log = logging.getLogger("radar_pncp")


@lru_cache(maxsize=1)
def configuracao_padrao():
    return carregar_configuracao()


def _configuracao(config):
    return configuracao_padrao() if config is None else config
