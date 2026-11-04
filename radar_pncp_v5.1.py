"""Consulta oportunidades do PNCP e gera a planilha diária do Radar v5.1."""

import argparse
import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import tempfile
import time
import unicodedata
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

URL = "https://pncp.gov.br/api/consulta/v1/contratacoes/proposta"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "licitacoes_progaia.db"
PASTA_PLANILHAS = BASE_DIR / "planilhas"
DIAS_MINIMOS = 7
LOCAIS_BLOQUEADOS = {("SP", "sao jose dos campos")}
FUSO_HORARIO = "America/Sao_Paulo"
VERSAO_REGRAS = "5.1.3"
VARIANTES_TERMOS = {
    "estudos ambientais": ("estudo ambiental",),
    "impactos ambientais": ("impacto ambiental",),
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


MAX_FALHAS_CONSECUTIVAS = 8
DELAY_ENTRE_REQUISICOES = 1.0
log = logging.getLogger("radar_pncp")


@lru_cache(maxsize=1)
def configuracao_padrao():
    return carregar_configuracao()


def _configuracao(config):
    return configuracao_padrao() if config is None else config


@lru_cache(maxsize=16)
def _filtros_compilados(exclusoes, resgate):
    return re.compile("|".join(exclusoes), re.I), re.compile("|".join(resgate), re.I)


TERMOS_CONTEXTUAIS = {
    "supervisão de obras",
    "supervisão das obras",
    "supervisão de obra",
    "assessoramento",
    "projeto básico e executivo de engenharia",
    "estudos e projetos de engenharia",
    "projeto de engenharia",
}
_CONTEXTO_INFRA = re.compile(
    r"\b(?:rodovia|br\s*-\s*\d+|ponte|ferrovia|hidrovia|duplica[çc][ãa]o|pavimenta[çc][ãa]o|rs[ct]?\s*-\s*\d+)\b",
    re.I,
)
_CONTEXTO_PLANO_MAN = re.compile(
    r"\b(?:ambiental|ambientais|florestal|florestais|fauna|flora|unidade de conservacao|area protegida)\b",
    re.I,
)


@lru_cache(maxsize=1024)
def _padrao_termo(termo, variantes=()):
    alternativas = []
    for texto in (termo, *variantes):
        tokens = [
            re.escape(p) + (r"(?:es)?" if p.endswith("r") else r"s?")
            for p in _normalizar(texto).split()
        ]
        alternativas.append(r"\b" + r"\s+".join(tokens) + r"\b")
    return re.compile("(?:" + "|".join(alternativas) + ")")


def _contem(texto, termo, variantes=True):
    extras = tuple(VARIANTES_TERMOS.get(termo, ())) if variantes else ()
    return _padrao_termo(termo, extras).search(_normalizar(texto)) is not None


@lru_cache(maxsize=16)
def _preparar_padroes(termos, familias, variantes):
    aliases = dict(variantes)
    tradicionais = tuple((t, _padrao_termo(t)) for t in termos)
    ampliados = tuple((t, _padrao_termo(t, aliases.get(t, ()))) for t in termos)
    topografia = tuple(_padrao_termo(t) for t in familias)
    termos_topograficos = frozenset(
        t for t in termos if any(p.search(_normalizar(t)) for p in topografia)
    )
    return tradicionais, ampliados, topografia, termos_topograficos


def _normalizar(texto):
    texto = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _local_bloqueado(uf, municipio):
    return (str(uf or "").strip().upper(), _normalizar(municipio).strip()) in LOCAIS_BLOQUEADOS


def _texto_para_classificar(objeto_compra, informacao_complementar=None):
    """Reúne objeto e informação complementar sem repetir o mesmo texto."""
    partes = []
    for valor in (objeto_compra, informacao_complementar):
        texto = str(valor or "").strip()
        if texto and texto not in partes:
            partes.append(texto)
    return " ".join(partes)


def _tem_familia_topografia(texto, config=None):
    config = _configuracao(config)
    return any(_contem(texto, termo) for termo in config["topografia_familia"])


def classificar(objeto_compra, detalhado=False, orgao=None, _usar_variantes=False, config=None):
    config = _configuracao(config)
    termos = config["termos"]
    familias = config["topografia_familia"]
    score_minimo = config["score_minimo"]
    regex_exclusao, regex_resgate = _filtros_compilados(
        tuple(config["exclusoes"]), tuple(config["resgate"])
    )
    texto = str(objeto_compra or "")
    normalizado = _normalizar(texto)
    contexto_orgao = bool(
        re.search(
            r"\b(?:dnit|der|daer|sinfra)\b|departamento nacional de infraestrutura de transportes",
            _normalizar(orgao or ""),
        )
    )
    contexto_infra = contexto_orgao or bool(_CONTEXTO_INFRA.search(normalizado))
    contexto_manejo = bool(_CONTEXTO_PLANO_MAN.search(normalizado))
    tradicionais, ampliados, familia, termos_topograficos = _preparar_padroes(
        tuple(termos), tuple(familias), tuple((t, tuple(v)) for t, v in VARIANTES_TERMOS.items())
    )
    tem_topografia = any(p.search(normalizado) for p in familia)
    modos = (ampliados,) if _usar_variantes else (tradicionais, ampliados)
    for padroes in modos:
        achados = [
            t
            for t, padrao in padroes
            if padrao.search(normalizado)
            and (t not in TERMOS_CONTEXTUAIS or contexto_infra)
            and (t != "plano de manejo" or contexto_manejo)
        ]
        if "eia/rima" in achados:
            achados = [t for t in achados if t not in ("eia", "rima")]
        termos_score = [t for t in achados if t not in termos_topograficos]
        bonus = "programas ambientais" in termos_score and contexto_infra
        score = sum(termos[t] for t in termos_score) + int(bonus)
        tem_forte = any(termos[t] >= 3 for t in termos_score) or bonus
        manter = bool(termos_score) and score >= score_minimo
        motivo = ""
        if tem_topografia and not any(termos[t] >= 2 for t in termos_score):
            manter = False
            motivo = "topografia/agrimensura sem serviço-alvo"
        if not manter and not motivo:
            motivo = "sem termo de relevância"
        if not tem_forte and regex_exclusao.search(texto) and not regex_resgate.search(texto):
            manter = False
            if not motivo:
                motivo = "compra de bens sem termo forte/serviço"
        principais = termos_score or achados
        principal = max(principais, key=lambda t: (termos[t], len(t))) if principais else ""
        resultado = {
            "manter": manter,
            "score": score,
            "termos": achados,
            "principal": principal,
            "motivo": motivo,
        }
        if manter:
            break
    if detalhado:
        return resultado
    return (score, achados, principal) if manter else None


def link_pncp(numero_controle):
    """Monta o endereço do edital a partir do número de controle PNCP."""
    correspondencia = re.match(r"^(\d{14})-\d-(\d+)/(\d{4})$", numero_controle or "")
    if not correspondencia:
        return None
    cnpj, sequencial, ano = correspondencia.groups()
    return f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{int(sequencial)}"


COLUNAS_NOVAS = {
    "progresso": [("assinatura", "TEXT"), ("rodada_id", "TEXT")],
    "paginas_coleta": [("rodada_id", "TEXT")],
    "licitacoes": [
        ("modalidadeNome", "TEXT"),
        ("palavrasChave", "TEXT"),
        ("score", "INTEGER"),
        ("primeiraColeta", "TEXT"),
        ("situacao", "TEXT"),
        ("dataAberturaProposta", "TEXT"),
        ("dataAtualizacaoPNCP", "TEXT"),
        ("ultimaAlteracao", "TEXT"),
        ("resumoAlteracoes", "TEXT"),
        ("descartada", "INTEGER DEFAULT 0"),
        ("motivoDescarte", "TEXT"),
        ("jaCaptou", "INTEGER DEFAULT 0"),
        ("ultimaRodada", "TEXT"),
    ],
    "log_execucoes": [
        ("total_escaneados", "INTEGER DEFAULT 0"),
        ("estado", "TEXT"),
        ("rodada_id", "TEXT"),
    ],
}


def iniciar_banco(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS metadados (chave TEXT PRIMARY KEY, valor TEXT)")
        conn.execute("""CREATE TABLE IF NOT EXISTS paginas_coleta (
            dia TEXT, uf TEXT, modalidade INTEGER, assinatura TEXT,
            inicio TEXT, fim TEXT, ultima_pagina INTEGER,
            PRIMARY KEY (dia,uf,modalidade))""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licitacoes (
                numeroControlePNCP TEXT PRIMARY KEY, palavraChaveEncontrada TEXT, objetoCompra TEXT,
                uf TEXT, municipio TEXT, orgao TEXT, modalidadeId INTEGER, modalidadeNome TEXT,
                dataEncerramentoProposta TEXT, valorTotalEstimado REAL, linkSistemaOrigem TEXT,
                dataColeta TEXT, dadosCompletos TEXT
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS log_execucoes (
                timestamp TEXT, total_escaneados INTEGER, total_encontradas INTEGER,
                total_falhas INTEGER, interrompido_por_circuito INTEGER
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS progresso (
                dia TEXT, uf TEXT, modalidade INTEGER, PRIMARY KEY (dia, uf, modalidade)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS historico_alteracoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, numeroControlePNCP TEXT NOT NULL,
                data TEXT NOT NULL, campo TEXT NOT NULL, valorAntigo TEXT, valorNovo TEXT
            )""")
        for tabela, colunas in COLUNAS_NOVAS.items():
            existentes = [r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")]
            for coluna, tipo in colunas:
                if coluna not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        # Preenche a primeira coleta dos registros anteriores à criação dessa coluna.
        conn.execute(
            "UPDATE licitacoes SET primeiraColeta = dataColeta WHERE primeiraColeta IS NULL"
        )
        conn.commit()
        return conn
    except BaseException:
        try:
            conn.rollback()
        finally:
            conn.close()
        raise


def assinatura_classificacao(config=None):
    config = _configuracao(config)
    dados = {
        "termos": config["termos"],
        "score_minimo": config["score_minimo"],
        "exclusoes": config["exclusoes"],
        "resgate": config["resgate"],
        "familia": config["topografia_familia"],
        "variantes": VARIANTES_TERMOS,
        "contextuais": sorted(TERMOS_CONTEXTUAIS),
        "infra": _CONTEXTO_INFRA.pattern,
        "manejo": _CONTEXTO_PLANO_MAN.pattern,
        "versao": VERSAO_REGRAS,
    }
    return hashlib.sha256(
        json.dumps(dados, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def reclassificar_banco(conn, tamanho_lote=200, config=None):
    config = _configuracao(config)
    if type(tamanho_lote) is not int or tamanho_lote <= 0:
        raise ValueError("O lote deve ser um inteiro positivo")
    assinatura = assinatura_classificacao(config)
    anterior = conn.execute("SELECT valor FROM metadados WHERE chave='classificacao'").fetchone()
    if anterior and anterior[0] == assinatura:
        return 0
    ultimo, descartadas = 0, 0
    while True:
        linhas = conn.execute(
            """SELECT rowid,numeroControlePNCP,objetoCompra,orgao,dadosCompletos,
            score,palavrasChave,palavraChaveEncontrada,descartada,motivoDescarte
            FROM licitacoes WHERE rowid>? ORDER BY rowid LIMIT ?""",
            (ultimo, tamanho_lote),
        ).fetchall()
        if not linhas:
            break
        for (
            rowid,
            numero,
            objeto,
            orgao,
            dados_completos,
            score,
            palavras,
            principal,
            descartada,
            motivo,
        ) in linhas:
            try:
                dados = json.loads(dados_completos or "{}")
            except (TypeError, ValueError):
                dados = {}
            dados = dados if isinstance(dados, dict) else {}
            unidade = dados.get("unidadeOrgao") or {}
            contexto = " ".join(str(v or "") for v in (orgao, unidade.get("nomeUnidade")))
            texto = _texto_para_classificar(objeto, dados.get("informacaoComplementar"))
            resultado = classificar(texto, detalhado=True, orgao=contexto, config=config)
            atual = (score, palavras, principal, descartada, motivo)
            if resultado["manter"]:
                novo = (
                    resultado["score"],
                    "; ".join(resultado["termos"]),
                    resultado["principal"],
                    0,
                    None,
                )
            else:
                novo = (score, palavras, principal, 1, resultado["motivo"])
                descartadas += 1
            if novo != atual:
                conn.execute(
                    """UPDATE licitacoes SET score=?,palavrasChave=?,palavraChaveEncontrada=?,
                    descartada=?,motivoDescarte=? WHERE numeroControlePNCP=?""",
                    (*novo, numero),
                )
            ultimo = rowid
    conn.execute(
        "INSERT INTO metadados(chave,valor) VALUES('classificacao',?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
        (assinatura,),
    )
    conn.commit()
    return descartadas


def _buscar_licitacao(conn, numero):
    cursor = conn.execute("SELECT * FROM licitacoes WHERE numeroControlePNCP=?", (numero,))
    linha = cursor.fetchone()
    if linha is None:
        return None
    colunas = [coluna[0] for coluna in cursor.description]
    return dict(zip(colunas, linha))


def _mesclar_dados(anteriores, novos):
    resultado = dict(anteriores)
    for chave, valor in novos.items():
        if valor is None:
            continue
        if isinstance(valor, dict) and isinstance(resultado.get(chave), dict):
            resultado[chave] = _mesclar_dados(resultado[chave], valor)
        else:
            resultado[chave] = valor
    return resultado


def _item_mesclado(atual, item):
    if not atual:
        return item
    try:
        dados = json.loads(atual.get("dadosCompletos") or "{}")
    except (TypeError, ValueError):
        dados = {}
    dados = dados if isinstance(dados, dict) else {}
    for origem, coluna in (
        ("objetoCompra", "objetoCompra"),
        ("modalidadeId", "modalidadeId"),
        ("dataEncerramentoProposta", "dataEncerramentoProposta"),
        ("dataAberturaProposta", "dataAberturaProposta"),
        ("situacaoCompraNome", "situacao"),
        ("valorTotalEstimado", "valorTotalEstimado"),
        ("linkSistemaOrigem", "linkSistemaOrigem"),
        ("dataAtualizacao", "dataAtualizacaoPNCP"),
    ):
        if atual.get(coluna) is not None:
            dados[origem] = atual[coluna]
    dados["unidadeOrgao"] = _mesclar_dados(
        dados.get("unidadeOrgao") or {},
        {
            k: v
            for k, v in {
                "ufSigla": atual.get("uf"),
                "municipioNome": atual.get("municipio"),
            }.items()
            if v is not None
        },
    )
    dados["orgaoEntidade"] = _mesclar_dados(
        dados.get("orgaoEntidade") or {},
        {"razaoSocial": atual["orgao"]} if atual.get("orgao") is not None else {},
    )
    return _mesclar_dados(dados, item)


def salvar_licitacao(conn, item, classificacao, atual=None, rodada_id=None, config=None):
    config = _configuracao(config)
    nomes_modalidades = {int(k): v for k, v in config["nomes_modalidades"].items()}
    numero = item.get("numeroControlePNCP")
    if not numero:
        return None
    atual = atual if atual is not None else _buscar_licitacao(conn, numero)
    item = _item_mesclado(atual, item)
    score, termos, principal = classificacao
    try:
        valor = (
            float(item["valorTotalEstimado"])
            if item.get("valorTotalEstimado") is not None
            else None
        )
    except (ValueError, TypeError):
        valor = None
    modalidade_id = item.get("modalidadeId")
    hoje = hoje_brasilia().isoformat()
    unidade = item.get("unidadeOrgao") or {}
    orgao = item.get("orgaoEntidade") or {}
    situacao = item.get("situacaoCompraNome")
    data_abertura = item.get("dataAberturaProposta")
    data_atualizacao = item.get("dataAtualizacao")
    novo = {
        "dataAberturaProposta": data_abertura,
        "dataEncerramentoProposta": item.get("dataEncerramentoProposta"),
        "situacao": situacao,
        "valorTotalEstimado": valor,
        "linkSistemaOrigem": item.get("linkSistemaOrigem"),
        "objetoCompra": item.get("objetoCompra"),
        "dataAtualizacaoPNCP": data_atualizacao,
        "orgao": orgao.get("razaoSocial"),
        "uf": unidade.get("ufSigla"),
        "municipio": unidade.get("municipioNome"),
        "modalidadeId": modalidade_id,
    }
    ja_existia = atual is not None
    alteracoes = []
    if atual:
        for campo, valor_novo in novo.items():
            # A data do PNCP pode mudar sem alteração material no edital.
            if campo == "dataAtualizacaoPNCP":
                continue
            valor_antigo = atual.get(campo)
            if campo == "valorTotalEstimado":
                # O PNCP também usa zero para valores não informados.
                if valor_novo in (None, 0):
                    valor_novo = valor_antigo
            elif valor_novo is None and valor_antigo is not None:
                valor_novo = valor_antigo
            if valor_novo != valor_antigo:
                alteracoes.append((campo, valor_antigo, valor_novo))
        for campo, antigo, novo_valor in alteracoes:
            conn.execute(
                "INSERT INTO historico_alteracoes (numeroControlePNCP,data,campo,valorAntigo,valorNovo) VALUES (?,?,?,?,?)",
                (numero, hoje, campo, _valor_texto(antigo), _valor_texto(novo_valor)),
            )
        resumo = atual.get("resumoAlteracoes") or ""
        if alteracoes:
            trechos = [
                f"{campo}: {_resumir_valor(a)} → {_resumir_valor(b)}" for campo, a, b in alteracoes
            ]
            linha = f"{hoje_brasilia():%d/%m/%Y}: " + "; ".join(trechos)
            resumo = (resumo + " | " + linha).strip(" |")
    else:
        resumo = None
    final = dict(novo)
    if atual:
        for campo, valor_novo in final.items():
            if valor_novo is None and atual.get(campo) is not None:
                final[campo] = atual[campo]
        if final["valorTotalEstimado"] in (None, 0) and atual.get("valorTotalEstimado") not in (
            None,
            0,
        ):
            final["valorTotalEstimado"] = atual["valorTotalEstimado"]
    conn.execute(
        """
        INSERT INTO licitacoes (
            numeroControlePNCP, palavraChaveEncontrada, palavrasChave, score, objetoCompra, uf, municipio,
            orgao, modalidadeId, modalidadeNome, dataEncerramentoProposta, valorTotalEstimado,
            linkSistemaOrigem, dataColeta, primeiraColeta, dadosCompletos, situacao,
            dataAberturaProposta, dataAtualizacaoPNCP, ultimaAlteracao, resumoAlteracoes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(numeroControlePNCP) DO UPDATE SET
            palavraChaveEncontrada=excluded.palavraChaveEncontrada,
            palavrasChave=excluded.palavrasChave, score=excluded.score,
            objetoCompra=excluded.objetoCompra, uf=excluded.uf, municipio=excluded.municipio,
            orgao=excluded.orgao, modalidadeId=excluded.modalidadeId, modalidadeNome=excluded.modalidadeNome,
            dataEncerramentoProposta=excluded.dataEncerramentoProposta,
            valorTotalEstimado=excluded.valorTotalEstimado, linkSistemaOrigem=excluded.linkSistemaOrigem,
            dataColeta=excluded.dataColeta, dadosCompletos=excluded.dadosCompletos,
            descartada=0, motivoDescarte=NULL,
            situacao=excluded.situacao, dataAberturaProposta=excluded.dataAberturaProposta,
            dataAtualizacaoPNCP=excluded.dataAtualizacaoPNCP,
            ultimaAlteracao=CASE WHEN excluded.ultimaAlteracao IS NOT NULL THEN excluded.ultimaAlteracao ELSE licitacoes.ultimaAlteracao END,
            resumoAlteracoes=COALESCE(excluded.resumoAlteracoes,licitacoes.resumoAlteracoes)
            -- primeiraColeta permanece fixa para identificar oportunidades novas.
    """,
        (
            numero,
            principal,
            "; ".join(termos),
            score,
            item.get("objetoCompra"),
            unidade.get("ufSigla"),
            unidade.get("municipioNome"),
            orgao.get("razaoSocial"),
            modalidade_id,
            nomes_modalidades.get(modalidade_id, f"Modalidade {modalidade_id}"),
            final["dataEncerramentoProposta"],
            final["valorTotalEstimado"],
            final["linkSistemaOrigem"],
            hoje,
            atual.get("primeiraColeta") if atual else hoje,
            json.dumps(item, ensure_ascii=False),
            final["situacao"],
            final["dataAberturaProposta"],
            final["dataAtualizacaoPNCP"],
            hoje if alteracoes else None,
            resumo,
        ),
    )
    if rodada_id:
        conn.execute(
            "UPDATE licitacoes SET ultimaRodada=? WHERE numeroControlePNCP=?", (rodada_id, numero)
        )
    return "ATUALIZADA" if alteracoes else ("SEM ALTERACAO" if ja_existia else "NOVA")


def _valor_texto(valor):
    return None if valor is None else str(valor)


def _resumir_valor(valor):
    if valor in (None, ""):
        return "(vazio)"
    return str(valor)[:80]


def registrar_execucao(
    conn, escaneados, encontradas, falhas, interrompido, estado=None, rodada_id=None
):
    estado = estado or (
        "completa" if not falhas and not interrompido else "parcial" if escaneados else "falhou"
    )
    conn.execute(
        """INSERT INTO log_execucoes
           (timestamp,total_escaneados,total_encontradas,total_falhas,interrompido_por_circuito,estado,rodada_id)
           VALUES (?,?,?,?,?,?,?)""",
        (
            agora_brasilia().isoformat(timespec="seconds"),
            escaneados,
            encontradas,
            falhas,
            int(interrompido),
            estado,
            rodada_id,
        ),
    )


def _validar_limites(*valores):
    if any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0
        for v in valores
    ):
        raise ValueError("Os limites de tempo devem ser finitos e positivos")


def segundos_retry_after(valor, agora=None):
    if valor is None:
        return None
    valor = str(valor).strip()
    if valor.isdecimal():
        return int(valor)
    try:
        data = parsedate_to_datetime(valor)
        if data.tzinfo is None:
            data = data.replace(tzinfo=timezone.utc)
        return max(0.0, (data - (agora or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def consultar_pagina(
    pagina,
    codigo_modalidade,
    uf,
    data_inicial,
    data_final,
    tentativas=4,
    timeout=20,
    metricas=None,
    sessao=None,
    limite_espera=60,
    prazo_consulta=120,
    prazo_execucao=None,
):
    propria = sessao is None
    sessao = sessao if sessao is not None else requests.Session()
    try:
        params = {
            "dataInicial": data_inicial,
            "dataFinal": data_final,
            "pagina": pagina,
            "codigoModalidadeContratacao": codigo_modalidade,
            "uf": uf,
            "tamanhoPagina": 50,
        }
        _validar_limites(timeout, limite_espera, prazo_consulta)
        if prazo_execucao is not None and not math.isfinite(prazo_execucao):
            raise ValueError("O prazo absoluto deve ser finito")
        prazo = min(
            time.monotonic() + prazo_consulta,
            prazo_execucao if prazo_execucao is not None else float("inf"),
        )
        for tentativa in range(tentativas):
            restante = prazo - time.monotonic()
            if restante <= 0:
                break
            resp = None
            espera_servidor = None
            try:
                inicio_requisicao = time.monotonic()
                try:
                    resp = sessao.get(URL, params=params, timeout=min(timeout, restante))
                finally:
                    if metricas is not None:
                        metricas["chamadas"] += 1
                        metricas["resposta_s"] += time.monotonic() - inicio_requisicao
                if resp.status_code == 429:
                    espera_servidor = segundos_retry_after(resp.headers.get("Retry-After"))
                    if espera_servidor is not None and espera_servidor > limite_espera:
                        log.error("Espera solicitada pelo servidor excede o teto; consulta adiada")
                        break
                elif resp.status_code == 204:
                    return None
                else:
                    resp.raise_for_status()
                    dados = resp.json()
                    if time.monotonic() >= prazo:
                        break
                    return dados
            except requests.exceptions.Timeout:
                log.warning("Timeout em %s/modalidade %s/página %s", uf, codigo_modalidade, pagina)
            except requests.exceptions.RequestException as erro:
                log.warning(
                    f"Erro de requisição ({uf}, modalidade {codigo_modalidade}, pág {pagina}): {erro}"
                )
            finally:
                if resp is not None:
                    resp.close()
            if tentativa + 1 >= tentativas:
                break
            base = min(limite_espera, 2 ** min(tentativa, 10))
            espera = min(limite_espera, base + random.uniform(0, base / 4))
            if espera_servidor is not None:
                espera = max(espera, espera_servidor)
            restante = prazo - time.monotonic()
            if restante <= espera:
                break
            if metricas is not None and espera_servidor is not None:
                metricas["espera_429_s"] += espera
            time.sleep(espera)
        return "FALHOU"
    finally:
        if propria:
            sessao.close()


def validar_pagina(resultado, pagina):
    if resultado is None:
        return None
    if not isinstance(resultado, dict) or not isinstance(resultado.get("data"), list):
        raise ValueError("Resposta deve conter uma lista data")
    total = resultado.get("totalPaginas")
    if type(total) is not int or total < 0:
        raise ValueError("totalPaginas ausente ou inválido")
    if resultado["data"] and total < pagina:
        raise ValueError("Paginação incompatível com os dados")
    if not resultado["data"] and total > pagina:
        raise ValueError("Página vazia antes do fim dos resultados")
    for item in resultado["data"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("numeroControlePNCP"), str)
            or not item["numeroControlePNCP"].strip()
        ):
            raise ValueError("Item sem número de controle válido")
        for campo in ("unidadeOrgao", "orgaoEntidade"):
            if item.get(campo) is not None and not isinstance(item[campo], dict):
                raise ValueError(f"{campo} deve ser um objeto")
    return resultado


def assinatura_configuracao(inicio=None, fim=None, config=None):
    parametros = {
        **_configuracao(config),
        "versao_regras": VERSAO_REGRAS,
        "locais_bloqueados": sorted(LOCAIS_BLOQUEADOS),
        "variantes": VARIANTES_TERMOS,
        "inicio": str(inicio),
        "fim": str(fim),
    }
    return hashlib.sha256(
        json.dumps(parametros, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def coletar(
    dias_futuros=90,
    delay=None,
    nova_varredura=False,
    prazo_total=2700,
    prazo_consulta=120,
    limite_espera=60,
    config=None,
    sessao=None,
    db_path=None,
):
    config = _configuracao(config)
    _validar_limites(prazo_total, prazo_consulta, limite_espera)
    ufs, modalidades = config["ufs"], config["modalidades"]
    propria = sessao is None
    sessao = sessao if sessao is not None else requests.Session()
    try:
        prazo_execucao = time.monotonic() + prazo_total
        delay = DELAY_ENTRE_REQUISICOES if delay is None else delay
        hoje = hoje_brasilia()
        dia = hoje.isoformat()
        data_final = hoje + timedelta(days=dias_futuros)
        conn = iniciar_banco(db_path)
        try:
            reclassificar_banco(conn, config=config)
            assinatura = assinatura_configuracao(hoje, data_final, config=config)
            conn.execute(
                "DELETE FROM progresso WHERE dia=? AND (assinatura IS NULL OR assinatura<>?)",
                (dia, assinatura),
            )
            conn.execute(
                "DELETE FROM paginas_coleta WHERE dia<>? OR assinatura<>?", (dia, assinatura)
            )
            conn.commit()
            if nova_varredura:
                conn.execute("DELETE FROM progresso WHERE dia = ?", (dia,))
                conn.execute("DELETE FROM paginas_coleta WHERE dia = ?", (dia,))
                conn.commit()
                feitos = set()
                log.info(
                    "Nova varredura completa solicitada; os checkpoints de hoje foram reiniciados."
                )
            else:
                feitos = {
                    (u, m)
                    for u, m in conn.execute(
                        "SELECT uf, modalidade FROM progresso WHERE dia = ?", (dia,)
                    )
                }
            anterior = conn.execute(
                """SELECT rodada_id FROM progresso WHERE dia=? AND assinatura=? AND rodada_id IS NOT NULL
                UNION ALL SELECT rodada_id FROM paginas_coleta WHERE dia=? AND assinatura=? AND rodada_id IS NOT NULL LIMIT 1""",
                (dia, assinatura, dia, assinatura),
            ).fetchone()
            rodada_id = anterior[0] if anterior else uuid4().hex
            total_escaneados = total_encontradas = total_falhas = falhas_consecutivas = 0
            novas = atualizadas = descartadas = 0
            vistos = set()
            existentes = {r[0] for r in conn.execute("SELECT numeroControlePNCP FROM licitacoes")}
            interrompido = False
            consultas_ok = 0

            log.info(f"Buscando propostas abertas de {hoje:%d/%m/%Y} até {data_final:%d/%m/%Y}.")
            if feitos:
                log.info(
                    f"Retomando: {len(feitos)} de {len(ufs) * len(modalidades)} combinações já coletadas hoje."
                )

            for uf in ufs:
                if interrompido:
                    break
                metricas_uf = {
                    "chamadas": 0,
                    "resposta_s": 0.0,
                    "espera_429_s": 0.0,
                    "pausa_s": 0.0,
                }
                inicio_uf = time.monotonic()
                for modalidade in modalidades:
                    if (uf, modalidade) in feitos:
                        continue
                    progresso = conn.execute(
                        "SELECT ultima_pagina FROM paginas_coleta WHERE dia=? AND uf=? AND modalidade=? AND assinatura=?",
                        (dia, uf, modalidade, assinatura),
                    ).fetchone()
                    pagina, combo_ok = (max(1, progresso[0]) if progresso else 1), True
                    while True:
                        if time.monotonic() >= prazo_execucao:
                            total_falhas += 1
                            interrompido = True
                            combo_ok = False
                            break
                        resultado = consultar_pagina(
                            pagina,
                            modalidade,
                            uf,
                            hoje.strftime("%Y%m%d"),
                            data_final.strftime("%Y%m%d"),
                            metricas=metricas_uf,
                            prazo_execucao=prazo_execucao,
                            prazo_consulta=prazo_consulta,
                            limite_espera=limite_espera,
                            sessao=sessao,
                        )
                        metricas_uf["pausa_s"] += delay
                        time.sleep(delay)
                        if resultado != "FALHOU":
                            try:
                                resultado = validar_pagina(resultado, pagina)
                            except ValueError as erro:
                                log.error(
                                    "Resposta inválida em %s/%s/página %s: %s",
                                    uf,
                                    modalidade,
                                    pagina,
                                    erro,
                                )
                                resultado = "FALHOU"
                        if resultado == "FALHOU":
                            total_falhas += 1
                            falhas_consecutivas += 1
                            combo_ok = False
                            if falhas_consecutivas >= MAX_FALHAS_CONSECUTIVAS:
                                log.error(
                                    f"{MAX_FALHAS_CONSECUTIVAS} falhas consecutivas — interrompendo. "
                                    "Rode de novo: a coleta continua de onde parou."
                                )
                                interrompido = True
                            break
                        falhas_consecutivas = 0
                        consultas_ok += 1
                        if resultado is None or not resultado.get("data"):
                            break
                        total_escaneados += len(resultado["data"])
                        for item in resultado["data"]:
                            numero = item.get("numeroControlePNCP")
                            if numero and numero in vistos:
                                continue
                            if numero:
                                vistos.add(numero)
                            atual = (
                                _buscar_licitacao(conn, numero) if numero in existentes else None
                            )
                            item = _item_mesclado(atual, item)
                            unidade = item.get("unidadeOrgao") or {}
                            if _local_bloqueado(
                                unidade.get("ufSigla"), unidade.get("municipioNome")
                            ):
                                continue
                            entidade = item.get("orgaoEntidade") or {}
                            texto_classificacao = _texto_para_classificar(
                                item.get("objetoCompra"), item.get("informacaoComplementar")
                            )
                            detalhes = classificar(
                                texto_classificacao,
                                detalhado=True,
                                config=config,
                                orgao=" ".join(
                                    str(v or "")
                                    for v in (
                                        entidade.get("razaoSocial"),
                                        unidade.get("nomeUnidade"),
                                    )
                                ),
                            )
                            if detalhes["manter"]:
                                movimento = salvar_licitacao(
                                    conn,
                                    item,
                                    (detalhes["score"], detalhes["termos"], detalhes["principal"]),
                                    atual=atual,
                                    rodada_id=rodada_id,
                                    config=config,
                                )
                                novas += movimento == "NOVA"
                                atualizadas += movimento == "ATUALIZADA"
                                total_encontradas += 1
                            elif (
                                atual
                                or detalhes["motivo"] == "topografia/agrimensura sem serviço-alvo"
                            ):
                                _salvar_descartada(
                                    conn,
                                    item,
                                    detalhes["motivo"],
                                    detalhes,
                                    atual,
                                    rodada_id,
                                    config=config,
                                )
                                descartadas += 1
                            existentes.add(numero)
                        conn.execute(
                            """INSERT INTO paginas_coleta
                            (dia,uf,modalidade,assinatura,inicio,fim,ultima_pagina,rodada_id) VALUES (?,?,?,?,?,?,?,?)
                            ON CONFLICT(dia,uf,modalidade) DO UPDATE SET ultima_pagina=excluded.ultima_pagina,
                            assinatura=excluded.assinatura,inicio=excluded.inicio,fim=excluded.fim,rodada_id=excluded.rodada_id""",
                            (
                                dia,
                                uf,
                                modalidade,
                                assinatura,
                                dia,
                                data_final.isoformat(),
                                pagina,
                                rodada_id,
                            ),
                        )
                        conn.commit()
                        if pagina >= resultado.get("totalPaginas", 1):
                            break
                        pagina += 1
                    if combo_ok:
                        conn.execute(
                            "INSERT OR IGNORE INTO progresso (dia,uf,modalidade,assinatura,rodada_id) VALUES (?,?,?,?,?)",
                            (dia, uf, modalidade, assinatura, rodada_id),
                        )
                        conn.execute(
                            "DELETE FROM paginas_coleta WHERE dia=? AND uf=? AND modalidade=?",
                            (dia, uf, modalidade),
                        )
                    if interrompido:
                        break
                conn.commit()
                duracao_uf = time.monotonic() - inicio_uf
                log.info(
                    f"{uf} concluído. Escaneadas: {total_escaneados} | duração: {duracao_uf:.1f}s | "
                    f"chamadas: {metricas_uf['chamadas']} ({metricas_uf['resposta_s']:.1f}s de resposta) | "
                    f"pausa normal: {metricas_uf['pausa_s']:.1f}s | espera 429: {metricas_uf['espera_429_s']:.1f}s"
                )

            estado = (
                "completa"
                if total_falhas == 0 and not interrompido
                else "parcial" if consultas_ok else "falhou"
            )
            registrar_execucao(
                conn,
                total_escaneados,
                total_encontradas,
                total_falhas,
                interrompido,
                estado,
                rodada_id,
            )
            conn.execute("DELETE FROM progresso WHERE dia < ?", (dia,))
            conn.commit()
            log.info(
                f"Coleta finalizada. {total_escaneados} escaneadas | {total_encontradas} relevantes | "
                f"{total_falhas} falhas | {estado}"
            )
            log.info(
                f"Movimentos na coleta: {novas} novas | {atualizadas} atualizadas | "
                f"{descartadas} descartadas por topografia isolada"
            )
            return {
                "estado": estado,
                "falhas": total_falhas,
                "escaneados": total_escaneados,
                "rodada_id": rodada_id,
            }
        finally:
            try:
                if conn.in_transaction:
                    conn.rollback()
            finally:
                conn.close()
    finally:
        if propria:
            sessao.close()


def _salvar_descartada(conn, item, motivo, detalhes=None, atual=None, rodada_id=None, config=None):
    detalhes = detalhes or {"score": 0, "termos": [], "principal": ""}
    salvar_licitacao(
        conn,
        item,
        (detalhes["score"], detalhes["termos"], detalhes["principal"]),
        atual=atual,
        rodada_id=rodada_id,
        config=config,
    )
    conn.execute(
        "UPDATE licitacoes SET descartada=1,motivoDescarte=? WHERE numeroControlePNCP=?",
        (motivo, item.get("numeroControlePNCP")),
    )


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
    texto = _normalizar(orgao)
    if "departamento nacional de infraestrutura de transportes" in texto or re.search(
        r"\bdnit\b", texto
    ):
        return True
    try:
        dados = json.loads(dados_completos or "{}")
    except (TypeError, ValueError):
        dados = {}
    unidade = dados.get("unidadeOrgao") or {}
    nome = _normalizar(unidade.get("nomeUnidade", ""))
    return "departamento nacional de infraestrutura de transportes" in nome or bool(
        re.search(r"\bdnit\b", nome)
    )


def _situacao_cancelada(valor):
    return _normalizar(valor).strip() in {
        "anulada",
        "anulado",
        "revogada",
        "revogado",
        "cancelada",
        "cancelado",
    }


def _situacao_suspensa(valor):
    return _normalizar(valor).strip() in {"suspensa", "suspenso"}


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


def montar_dataframe(conn, hoje=None, incluir_sem_interesse=False, config=None):
    config = _configuracao(config)
    rotulos_captacao = {int(k): v for k, v in config["rotulos_captacao"].items()}
    agora_brt = agora_brasilia().tz_localize(None)
    hoje = pd.Timestamp(hoje or agora_brt.date()).normalize()
    # O ID da planilha referencia o rowid enquanto o registro permanecer no banco.
    df = pd.read_sql_query(
        """SELECT rowid AS idRadar,numeroControlePNCP,objetoCompra,uf,municipio,
        orgao,modalidadeNome,dataEncerramentoProposta,valorTotalEstimado,dataColeta,primeiraColeta,
        dadosCompletos,situacao,dataAberturaProposta,ultimaAlteracao,descartada,jaCaptou,score,
        palavrasChave,ultimaRodada FROM licitacoes""",
        conn,
    )
    locais_bloqueados = pd.Series(
        [_local_bloqueado(uf, municipio) for uf, municipio in zip(df["uf"], df["municipio"])],
        index=df.index,
        dtype=bool,
    )
    df = df[~locais_bloqueados].copy()
    df["encerramento"] = pd.to_datetime(
        df["dataEncerramentoProposta"].map(_timestamp_brasilia), errors="coerce"
    )
    df["descartada"] = df["descartada"].fillna(0).astype(int)
    df["dnit"] = df.apply(
        lambda r: "DNIT" if _eh_dnit(r.get("orgao"), r.get("dadosCompletos")) else "", axis=1
    )
    ainda_aberta = df["encerramento"] >= agora_brt
    df_aberto = df[(df["descartada"] == 0) & ainda_aberta].copy()
    if not incluir_sem_interesse:
        df_aberto = df_aberto[df_aberto["jaCaptou"].fillna(0).astype(int) != 2].copy()
    df_aberto["jaCaptou"] = (
        df_aberto["jaCaptou"]
        .fillna(0)
        .astype(int)
        .map(lambda valor: rotulos_captacao.get(valor, "Não"))
    )
    df_aberto["idVisivel"] = df_aberto["idRadar"].map(
        lambda valor: f"R-{int(valor):06d}" if pd.notna(valor) else ""
    )
    extras = df_aberto["dadosCompletos"].map(_informacoes_apresentacao)
    df_aberto["tipoPublicacao"] = extras.map(lambda valor: valor[0])
    df_aberto["fonteOrcamentaria"] = extras.map(lambda valor: valor[1])
    df_aberto["dias_restantes"] = (df_aberto["encerramento"].dt.normalize() - hoje).dt.days
    excecao_status = df_aberto["situacao"].map(
        lambda valor: _situacao_cancelada(valor) or _situacao_suspensa(valor)
    )
    df_aberto = df_aberto[(df_aberto["dias_restantes"] >= DIAS_MINIMOS) | excecao_status].copy()
    df_aberto["movimento"] = ""
    df_aberto.loc[df_aberto["primeiraColeta"] == hoje.date().isoformat(), "movimento"] = "NOVA"
    df_aberto.loc[
        (df_aberto["movimento"] == "") & (df_aberto["ultimaAlteracao"] == hoje.date().isoformat()),
        "movimento",
    ] = "ATUALIZADA"
    ultima_completa = conn.execute("""SELECT timestamp,rodada_id FROM log_execucoes
        WHERE (estado='completa' OR (estado IS NULL AND COALESCE(interrompido_por_circuito,0)=0
        AND COALESCE(total_falhas,0)=0)) ORDER BY timestamp DESC,rowid DESC LIMIT 1""").fetchone()
    if ultima_completa:
        data_execucao = str(ultima_completa[0])[:10]
        nao_vista = (df_aberto["dataColeta"].fillna("") < data_execucao) & (
            df_aberto["movimento"] == ""
        )
        if ultima_completa[1]:
            nao_vista = df_aberto["ultimaRodada"].fillna("") != ultima_completa[1]
        df_aberto.loc[nao_vista, "movimento"] = "NÃO VISTA NA ÚLTIMA COLETA"
    df_aberto["valorTotalEstimado"] = df_aberto["valorTotalEstimado"].astype(object)
    df_aberto.loc[
        df_aberto["valorTotalEstimado"].isna() | (df_aberto["valorTotalEstimado"] == 0),
        "valorTotalEstimado",
    ] = "não informado"
    df_aberto["linkPNCP"] = df_aberto["numeroControlePNCP"].apply(link_pncp)
    df_aberto["urlDestino"] = df_aberto["linkPNCP"]
    df_aberto["abrirEdital"] = df_aberto["urlDestino"].map(
        lambda valor: (
            "Abrir edital"
            if isinstance(valor, str) and valor.startswith(("http://", "https://"))
            else ""
        )
    )
    df_aberto = df_aberto.sort_values(
        ["dnit", "dias_restantes", "score"], ascending=[False, True, False]
    )
    colunas = [
        "dnit",
        "idVisivel",
        "jaCaptou",
        "movimento",
        "dias_restantes",
        "dataEncerramentoProposta",
        "objetoCompra",
        "orgao",
        "uf",
        "municipio",
        "tipoPublicacao",
        "valorTotalEstimado",
        "modalidadeNome",
        "fonteOrcamentaria",
        "score",
        "situacao",
        "dataAberturaProposta",
        "palavrasChave",
        "primeiraColeta",
        "dataColeta",
        "abrirEdital",
        "urlDestino",
        "numeroControlePNCP",
    ]
    df_principal = df_aberto[colunas].copy()
    df_principal = df_principal.rename(columns={"jaCaptou": "Já captei?"})
    data_cols = ["dataEncerramentoProposta", "dataAberturaProposta", "primeiraColeta", "dataColeta"]
    for coluna in data_cols:
        df_principal[coluna] = df_principal[coluna].map(_timestamp_brasilia)
    for coluna in df_principal.columns:
        if coluna not in data_cols:
            df_principal[coluna] = df_principal[coluna].map(limpar_para_excel)
    return df_principal


def _baseline_marcacoes(livro):
    try:
        valor = livro.custom_doc_props["RadarMarcacoes"].value
    except KeyError:
        return None
    try:
        dados = json.loads(valor)
        if dados["versao"] != 1 or not isinstance(dados["linhas"], dict):
            raise ValueError("Versão de marcações inválida")
        return dados["linhas"]
    except (ValueError, TypeError, KeyError) as erro:
        raise ValueError("Metadados de marcações inválidos; importação cancelada") from erro


def importar_marcacoes_captacao(conn, caminho, exigir_baseline=False, config=None):
    config = _configuracao(config)
    status_captacao = config["status_captacao"]
    from openpyxl import load_workbook

    livro = load_workbook(caminho, read_only=True, data_only=True)
    try:
        baseline = _baseline_marcacoes(livro)
        if exigir_baseline and baseline is None:
            log.warning(
                "Planilha legada sem referência: use --importar-marcacoes para importar explicitamente"
            )
            return 0
        propostas, estados, referencias = {}, {}, {}
        for nome in ("Em Aberto", "DNIT", "Novas", "Atualizadas", "Já captei", "Sem interesse"):
            if nome not in livro.sheetnames:
                continue
            aba = livro[nome]
            colunas = {str(c.value or "").strip(): i for i, c in enumerate(aba[1])}
            if "Já captei?" not in colunas:
                continue
            for linha in aba.iter_rows(min_row=2, values_only=True):
                status = _normalizar(linha[colunas["Já captei?"]]).strip()
                if not status:
                    continue
                if status not in status_captacao:
                    raise ValueError(f"Marcação inválida na aba {nome}")
                identificador = str(linha[colunas["ID"]] or "") if "ID" in colunas else ""
                referencia = baseline.get(identificador) if baseline is not None else None
                if baseline is not None and referencia is None:
                    raise ValueError("ID sem referência; importação cancelada")
                numero = referencia.get("numero") if referencia else None
                if not numero and "numeroControlePNCP" in colunas:
                    numero = str(linha[colunas["numeroControlePNCP"]] or "")
                if not numero:
                    correspondencia = re.fullmatch(r"R-(\d+)", identificador)
                    if not correspondencia:
                        continue
                    atual = conn.execute(
                        "SELECT numeroControlePNCP FROM licitacoes WHERE rowid=?",
                        (int(correspondencia.group(1)),),
                    ).fetchone()
                    numero = atual[0] if atual else None
                if not numero:
                    continue
                if numero not in estados:
                    atual = conn.execute(
                        "SELECT COALESCE(jaCaptou,0) FROM licitacoes WHERE numeroControlePNCP=?",
                        (numero,),
                    ).fetchone()
                    if atual is None:
                        continue
                    estados[numero] = atual[0]
                anterior = referencia["status"] if referencia else estados[numero]
                valor = status_captacao[status]
                if valor != anterior:
                    propostas.setdefault(numero, set()).add(valor)
                    referencias[numero] = anterior
        plano = []
        for numero, escolhas in propostas.items():
            if len(escolhas) != 1:
                raise ValueError(
                    f"Conflito entre abas para {numero}; nenhuma marcação foi aplicada"
                )
            valor = next(iter(escolhas))
            if estados[numero] not in (referencias[numero], valor):
                raise ValueError(f"Conflito com decisão mais recente para {numero}")
            if valor != estados[numero]:
                plano.append((valor, numero, estados[numero]))
        conn.execute("SAVEPOINT marcacoes")
        try:
            for valor, numero, anterior in plano:
                cur = conn.execute(
                    "UPDATE licitacoes SET jaCaptou=? WHERE numeroControlePNCP=? AND COALESCE(jaCaptou,0)=?",
                    (valor, numero, anterior),
                )
                if cur.rowcount != 1:
                    raise ValueError("Conflito de edição durante a importação")
            conn.execute("RELEASE SAVEPOINT marcacoes")
            conn.commit()
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT marcacoes")
            conn.execute("RELEASE SAVEPOINT marcacoes")
            raise
        return len(plano)
    finally:
        livro.close()


def _origem_marcacoes(destino, hoje):
    if destino.exists():
        return destino
    candidatos = []
    for caminho in destino.parent.glob("*.xlsx"):
        try:
            dia = datetime.strptime(caminho.stem, "%d.%m.%Y").date()
        except ValueError:
            continue
        if dia <= hoje:
            candidatos.append((dia, caminho))
    return max(candidatos, key=lambda item: item[0])[1] if candidatos else None


def exportar_excel(db_path=None, excel_path=None, hoje=None, importar_de=None, config=None):
    config = _configuracao(config)
    conn = iniciar_banco(db_path)
    try:
        data_hoje = pd.Timestamp(hoje or hoje_brasilia()).date()
        destino = (
            Path(excel_path) if excel_path else Path(PASTA_PLANILHAS) / f"{data_hoje:%d.%m.%Y}.xlsx"
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        planilha_origem = (
            Path(importar_de) if importar_de else _origem_marcacoes(destino, data_hoje)
        )
        if planilha_origem:
            quantidade = importar_marcacoes_captacao(
                conn, planilha_origem, exigir_baseline=not bool(importar_de), config=config
            )
            if quantidade:
                log.info(
                    f"Marcações 'Já captei?' importadas de {planilha_origem.name}: {quantidade}"
                )
        conn.commit()
        base = montar_dataframe(conn, hoje=data_hoje, incluir_sem_interesse=True, config=config)
        df = base[base["Já captei?"] != "Sem interesse"].copy()
        df_sem_interesse = base[base["Já captei?"] == "Sem interesse"].copy()
        df_ja_captei = df[df["Já captei?"] == "Sim"]
        novas = df[df["movimento"] == "NOVA"]
        atualizadas = df[df["movimento"] == "ATUALIZADA"]
        nao_vistas = df[df["movimento"] == "NÃO VISTA NA ÚLTIMA COLETA"]
        dnit = df[df["dnit"] == "DNIT"]
        tabelas = [
            ("Em Aberto", df),
            ("DNIT", dnit),
            ("Novas", novas),
            ("Atualizadas", atualizadas),
            ("Já captei", df_ja_captei),
            ("Sem interesse", df_sem_interesse),
        ]
        try:
            _gravar_planilha(destino, tabelas, config=config)
        except PermissionError:
            raise PermissionError(
                f"Não foi possível salvar {destino}. Feche o arquivo no Excel e tente novamente."
            )
        log.info(
            f"Excel salvo em {destino}: {len(novas)} novas | {len(atualizadas)} atualizadas | "
            f"{len(nao_vistas)} não vistas"
        )
    finally:
        try:
            if conn.in_transaction:
                conn.rollback()
        finally:
            conn.close()


def _gravar_planilha(destino, tabelas, config=None):
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destino.parent, prefix=f".{destino.stem}-", suffix=".xlsx", delete=False
    ) as arquivo:
        temporario = Path(arquivo.name)
    try:
        _escrever_planilha(temporario, tabelas, config=config)
        from openpyxl import load_workbook

        livro = load_workbook(temporario, read_only=True)
        try:
            if livro.sheetnames != [nome for nome, _ in tabelas]:
                raise ValueError("Planilha gerada com abas incompletas")
        finally:
            livro.close()
        os.replace(temporario, destino)
    finally:
        temporario.unlink(missing_ok=True)


def _escrever_planilha(destino, tabelas, config=None):
    config = _configuracao(config)
    status_captacao = config["status_captacao"]
    from openpyxl.packaging.custom import StringProperty
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    cores = {"NOVA": "C6EFCE", "ATUALIZADA": "FFEB9C", "NÃO VISTA NA ÚLTIMA COLETA": "D9D9D9"}
    cor_ja_captei = "DDEBF7"
    rotulos = {
        "dnit": "Prioridade",
        "idVisivel": "ID",
        "movimento": "Atualização",
        "dias_restantes": "Dias restantes",
        "dataEncerramentoProposta": "Fim das propostas",
        "objetoCompra": "Objeto",
        "orgao": "Órgão",
        "uf": "UF",
        "municipio": "Município",
        "tipoPublicacao": "Tipo de publicação",
        "valorTotalEstimado": "Valor estimado",
        "modalidadeNome": "Modalidade",
        "fonteOrcamentaria": "Fonte orçamentária",
        "score": "Score",
        "situacao": "Situação",
        "dataAberturaProposta": "Início das propostas",
        "palavrasChave": "Termos encontrados",
        "primeiraColeta": "Primeira identificação",
        "dataColeta": "Última coleta",
        "abrirEdital": "Abrir edital",
    }
    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        referencias = {}
        for _, quadro in tabelas:
            if {"idVisivel", "numeroControlePNCP", "Já captei?"} <= set(quadro.columns):
                for identificador, numero, status in zip(
                    quadro["idVisivel"], quadro["numeroControlePNCP"], quadro["Já captei?"]
                ):
                    referencias[identificador] = {
                        "numero": numero,
                        "status": status_captacao[_normalizar(status)],
                    }
        writer.book.custom_doc_props.append(
            StringProperty(
                name="RadarMarcacoes",
                value=json.dumps({"versao": 1, "linhas": referencias}, ensure_ascii=False),
            )
        )
        for aba_nome, quadro in tabelas:
            colunas_internas = ["numeroControlePNCP", "urlDestino"]
            quadro.drop(columns=colunas_internas, errors="ignore").rename(columns=rotulos).to_excel(
                writer, sheet_name=aba_nome, index=False
            )
            aba = writer.sheets[aba_nome]
            aba.freeze_panes = "A2"
            aba.auto_filter.ref = aba.dimensions
            indices = {c.value: c.column for c in aba[1]}
            if "Já captei?" in indices and aba.max_row > 1:
                coluna_captacao = indices["Já captei?"]
                validacao = DataValidation(
                    type="list", formula1='"Não,Sim,Sem interesse"', allow_blank=False
                )
                validacao.error = "Escolha uma das três opções da lista."
                validacao.errorTitle = "Valor inválido"
                validacao.prompt = "Escolha Sim, Não ou Sem interesse para este edital."
                validacao.promptTitle = "Controle de captação"
                aba.add_data_validation(validacao)
                validacao.add(
                    f"{aba.cell(2, coluna_captacao).coordinate}:"
                    f"{aba.cell(aba.max_row, coluna_captacao).coordinate}"
                )
            larguras = {
                "Objeto": 70,
                "Órgão": 35,
                "Modalidade": 26,
                "Termos encontrados": 32,
                "Prioridade": 12,
                "Atualização": 25,
                "Já captei?": 14,
                "ID": 12,
                "Tipo de publicação": 22,
                "Fonte orçamentária": 48,
                "Abrir edital": 18,
                "Dias restantes": 16,
                "Valor estimado": 18,
            }
            for nome, largura in larguras.items():
                if nome in indices:
                    aba.column_dimensions[aba.cell(1, indices[nome]).column_letter].width = largura
            for linha in range(2, aba.max_row + 1):
                aba.row_dimensions[linha].height = 45
                for nome in (
                    "Fim das propostas",
                    "Início das propostas",
                    "Primeira identificação",
                    "Última coleta",
                ):
                    if nome in indices:
                        celula_data = aba.cell(linha, indices[nome])
                        valor_data = celula_data.value
                        tem_hora = isinstance(valor_data, (datetime, pd.Timestamp)) and bool(
                            valor_data.hour or valor_data.minute or valor_data.second
                        )
                        celula_data.number_format = "dd/mm/yyyy hh:mm" if tem_hora else "dd/mm/yyyy"
                for nome in ("Objeto", "Fonte orçamentária"):
                    if nome in indices:
                        aba.cell(linha, indices[nome]).alignment = Alignment(
                            wrap_text=True, vertical="center"
                        )
                if "Valor estimado" in indices:
                    valor = aba.cell(linha, indices["Valor estimado"])
                    if isinstance(valor.value, (int, float)):
                        valor.number_format = "R$ #,##0.00"
                if (
                    "Abrir edital" in indices
                    and "urlDestino" in quadro.columns
                    and linha - 2 < len(quadro)
                ):
                    celula_link = aba.cell(linha, indices["Abrir edital"])
                    alvo = quadro["urlDestino"].iloc[linha - 2]
                    if isinstance(alvo, str) and alvo.startswith(("http://", "https://")):
                        celula_link.value = "Abrir edital"
                        celula_link.hyperlink = alvo
                        celula_link.style = "Hyperlink"
            if "movimento" in quadro.columns:
                idx_movimento = indices["Atualização"]
                idx_situacao = indices["Situação"]
                for linha in range(2, aba.max_row + 1):
                    movimento = aba.cell(linha, idx_movimento).value
                    situacao = aba.cell(linha, idx_situacao).value
                    cancelada = _situacao_cancelada(situacao)
                    suspensa = _situacao_suspensa(situacao)
                    destaque_vermelho = cancelada or suspensa
                    ja_captei = (
                        "Já captei?" in indices
                        and aba.cell(linha, indices["Já captei?"]).value == "Sim"
                    )
                    cor = (
                        "FFC7CE"
                        if destaque_vermelho
                        else (cor_ja_captei if ja_captei else cores.get(movimento))
                    )
                    if cor:
                        preenchimento = PatternFill(fill_type="solid", fgColor=cor)
                        for celula in aba[linha]:
                            celula.fill = preenchimento
                            if destaque_vermelho:
                                celula.font = Font(color="9C0006")
                    if (
                        "Prioridade" in indices
                        and aba.cell(linha, indices["Prioridade"]).value == "DNIT"
                    ):
                        celula = aba.cell(linha, indices["Prioridade"])
                        celula.font = Font(
                            bold=True, color="9C0006" if destaque_vermelho else "FFFFFF"
                        )
                        if not destaque_vermelho:
                            celula.fill = PatternFill(fill_type="solid", fgColor="1F4E78")
            grade = Side(style="thin", color="A6A6A6")
            borda = Border(left=grade, right=grade, top=grade, bottom=grade)
            for linha in aba.iter_rows():
                for celula in linha:
                    if isinstance(celula.value, str):
                        celula.data_type = "s"
                    celula.alignment = Alignment(
                        horizontal="center", vertical="center", wrap_text=True
                    )
                    celula.border = borda


def main():
    parser = argparse.ArgumentParser(
        description="Coleta oportunidades do PNCP e exporta a planilha diária."
    )
    parser.add_argument(
        "--nova-varredura",
        action="store_true",
        help="consulta novamente todas as combinações do dia",
    )
    parser.add_argument("--limite-espera", type=float, default=60)
    parser.add_argument("--prazo-consulta", type=float, default=120)
    parser.add_argument("--prazo-total", type=float, default=2700)
    parser.add_argument("--importar-marcacoes", type=Path)
    parser.add_argument("--configuracao", type=Path, default=BASE_DIR / "configuracao.json")
    parser.add_argument("--validar-configuracao", action="store_true")
    argumentos = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
    )
    try:
        config = carregar_configuracao(argumentos.configuracao)
        _validar_limites(
            argumentos.limite_espera, argumentos.prazo_consulta, argumentos.prazo_total
        )
    except ValueError as erro:
        log.error("Erro de configuração: %s", erro)
        return 2
    if argumentos.validar_configuracao:
        print("Configuração válida")
        return 0
    sessao = requests.Session()
    try:
        resultado = coletar(
            dias_futuros=90,
            nova_varredura=argumentos.nova_varredura,
            limite_espera=argumentos.limite_espera,
            prazo_consulta=argumentos.prazo_consulta,
            prazo_total=argumentos.prazo_total,
            config=config,
            sessao=sessao,
        )
        if resultado["estado"] != "completa":
            log.error("Coleta %s; a planilha anterior foi preservada", resultado["estado"])
            return 2 if resultado["estado"] == "parcial" else 1
        exportar_excel(importar_de=argumentos.importar_marcacoes, config=config)
        return 0
    except (ValueError, OSError, sqlite3.Error) as erro:
        log.error("Execução não concluída: %s", erro)
        return 1
    finally:
        sessao.close()


if __name__ == "__main__":
    raise SystemExit(main())
