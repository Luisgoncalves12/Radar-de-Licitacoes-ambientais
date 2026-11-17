"""Identificacao, datas e situacoes das licitacoes."""

import json
import re

import pandas as pd

from . import classificacao as classificacao_mod


def _identificar_edital(numero_controle):
    correspondencia = re.match(r"^(\d{14})-\d-(\d+)/(\d{4})$", numero_controle or "")
    if not correspondencia:
        return None
    cnpj, sequencial, ano = correspondencia.groups()
    return {"cnpj": cnpj, "ano": ano, "sequencial": int(sequencial)}


def link_pncp(numero_controle):
    """Monta o endereço do edital a partir do número de controle PNCP."""
    edital = _identificar_edital(numero_controle)
    if edital:
        return "https://pncp.gov.br/app/editais/{cnpj}/{ano}/{sequencial}".format(**edital)


def limpar_para_excel(texto):
    if not isinstance(texto, str):
        return texto
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", texto)


def _timestamp_brasilia(valor):
    if not valor:
        return pd.NaT
    try:
        ts = pd.Timestamp(valor)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("America/Sao_Paulo").tz_localize(None)
        return ts
    except (ValueError, TypeError):
        return pd.NaT


def _eh_dnit(orgao, dados_completos):
    texto = classificacao_mod._normalizar(orgao)
    if "departamento nacional de infraestrutura de transportes" in texto or re.search(
        r"\bdnit\b", texto
    ):
        return True
    try:
        dados = json.loads(dados_completos or "{}")
    except (TypeError, ValueError):
        dados = {}
    dados = dados if isinstance(dados, dict) else {}
    unidade = dados.get("unidadeOrgao") or {}
    unidade = unidade if isinstance(unidade, dict) else {}
    nome = classificacao_mod._normalizar(unidade.get("nomeUnidade", ""))
    return "departamento nacional de infraestrutura de transportes" in nome or bool(
        re.search(r"\bdnit\b", nome)
    )


SITUACOES_CANCELADAS = ("anulada", "anulado", "revogada", "revogado", "cancelada", "cancelado")
SITUACOES_SUSPENSAS = ("suspensa", "suspenso")


def orgao_preferencial(orgao, dados_completos):
    if _eh_dnit(orgao, dados_completos):
        return "DNIT"
    try:
        dados = json.loads(dados_completos or "{}")
    except (ValueError, TypeError):
        dados = {}
    dados = dados if isinstance(dados, dict) else {}
    unidade = dados.get("unidadeOrgao") or {}
    unidade = unidade if isinstance(unidade, dict) else {}
    texto = classificacao_mod._normalizar(f"{orgao or ''} {unidade.get('nomeUnidade', '')}")
    if re.search(r"\bcodevasf\b|companhia de desenvolvimento (?:dos? vales?|do v\.)", texto):
        return "CODEVASF"
    return ""


def prioridade_servico(objeto, informacao_complementar=None):
    """Prioriza o escopo contratado, independentemente do órgão e do score."""
    texto = classificacao_mod._normalizar(
        classificacao_mod._texto_para_classificar(objeto, informacao_complementar)
    )
    texto = classificacao_mod._sem_mencoes_incidentais(
        classificacao_mod._sem_termos_administrativos(texto)
    )
    if re.search(
        r"\b(?:supervisao|consultoria|gerenciamento|gestao|acompanhamento)"
        r"(?:\s+(?:e|de|da|do|tecnica|servicos?|engenharia|supervisao|consultoria))*\s+ambiental\b"
        r"|\b(?:supervisao|acompanhamento|execucao|gerenciamento)\s+"
        r"(?:de\s+)?programas? ambientais\b",
        texto,
    ):
        return "1 — Supervisão e consultoria ambiental"
    if re.search(
        r"\b(?:manejo arboreo|arborizacao|poda|podas|destoca|destocamento|"
        r"supressao (?:vegetal|arborea|de vegetacao|de arvores)|"
        r"manejo de arvores)\b", texto
    ):
        return "2 — Arbóreo"
    return "3 — Outros serviços de interesse"


def _situacao_cancelada(valor):
    return classificacao_mod._normalizar(valor).strip() in SITUACOES_CANCELADAS


def _situacao_suspensa(valor):
    return classificacao_mod._normalizar(valor).strip() in SITUACOES_SUSPENSAS


def _informacoes_apresentacao(dados_completos):
    """Extrai o tipo de publicação e as fontes orçamentárias do JSON salvo."""
    try:
        dados = json.loads(dados_completos or "{}")
    except (TypeError, ValueError):
        dados = {}
    tipo = dados.get("tipoInstrumentoConvocatorioNome") or ""
    fontes = dados.get("fontesOrcamentarias") or []
    descricoes = []
    for fonte in fontes:
        if not isinstance(fonte, dict):
            continue
        nome = str(fonte.get("nome") or "").strip()
        descricao = str(fonte.get("descricao") or "").strip()
        trecho = " — ".join(parte for parte in (nome, descricao) if parte)
        if trecho and trecho not in descricoes:
            descricoes.append(trecho)
    return tipo, " | ".join(descricoes)
