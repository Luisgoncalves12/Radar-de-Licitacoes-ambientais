"""Persistencia SQLite, checkpoints e historico de alteracoes."""

from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
import errno
import json
import os
import sqlite3
import threading

import pandas as pd

from . import classificacao as classificacao_mod
from . import configuracao as configuracao_mod
from . import dados as dados_mod


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
        ("motivoDesinteresse", "TEXT NOT NULL DEFAULT ''"),
        ("ultimaRodada", "TEXT"),
        ("dataConsultaSituacao", "TEXT"),
        ("dataTentativaSituacao", "TEXT"),
        ("dataMovimentacaoPNCP", "TEXT"),
    ],
    "historico_marcacoes": [
        ("motivoAntigo", "TEXT NOT NULL DEFAULT ''"),
        ("motivoNovo", "TEXT NOT NULL DEFAULT ''"),
    ],
    "log_execucoes": [
        ("total_escaneados", "INTEGER DEFAULT 0"),
        ("estado", "TEXT"),
        ("rodada_id", "TEXT"),
        ("dia_coleta", "TEXT"),
        ("total_chamadas", "INTEGER"),
        ("combinacoes_pendentes", "INTEGER"),
        ("assinatura", "TEXT"),
        ("total_combinacoes", "INTEGER"),
    ],
}
_bloqueios_locais = threading.local()


@contextmanager
def _bloquear_banco(db_path=None):
    caminho = db_path or configuracao_mod.DB_PATH
    if caminho == ":memory:":
        yield
        return
    caminho = Path(caminho).resolve()
    bloqueados = _bloqueios_locais.__dict__.setdefault("caminhos", set())
    if caminho in bloqueados:
        yield
        return
    # O sistema libera o bloqueio ao fechar o arquivo, inclusive se o Radar cair.
    with caminho.with_name(f"{caminho.name}.radar.lock").open("a+b") as arquivo:
        try:
            if os.name == "nt":
                import msvcrt

                arquivo.seek(0)
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(arquivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as erro:
            if erro.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise
            raise ValueError(f"Outra execução do Radar está usando o banco {caminho}") from erro
        bloqueados.add(caminho)
        try:
            yield
        finally:
            bloqueados.remove(caminho)


def iniciar_banco(db_path=None):
    conn = sqlite3.connect(db_path or configuracao_mod.DB_PATH)
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS historico_marcacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, numeroControlePNCP TEXT NOT NULL,
                data TEXT NOT NULL, valorAntigo INTEGER NOT NULL, valorNovo INTEGER NOT NULL,
                origem TEXT NOT NULL, reverte_id INTEGER
            )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS historico_marcacoes_edital
                        ON historico_marcacoes(numeroControlePNCP, id)""")
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


def reclassificar_banco(conn, tamanho_lote=200, config=None, dia_coleta=None):
    config = configuracao_mod._configuracao(config)
    if type(tamanho_lote) is not int or tamanho_lote <= 0:
        raise ValueError("O lote deve ser um inteiro positivo")
    assinatura = classificacao_mod.assinatura_classificacao(config)
    anterior = conn.execute("SELECT valor FROM metadados WHERE chave='classificacao'").fetchone()
    if anterior and anterior[0] == assinatura:
        return 0
    dia = (dia_coleta or configuracao_mod.hoje_brasilia()).isoformat()
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
            texto = classificacao_mod._texto_para_classificar(objeto, dados.get("informacaoComplementar"))
            resultado = classificacao_mod.classificar(texto, detalhado=True, orgao=contexto, config=config)
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
                reativada = descartada == 1 and resultado["manter"]
                conn.execute(
                    """UPDATE licitacoes SET score=?,palavrasChave=?,palavraChaveEncontrada=?,
                    descartada=?,motivoDescarte=?,ultimaAlteracao=COALESCE(?,ultimaAlteracao)
                    WHERE numeroControlePNCP=?""",
                    (*novo, dia if reativada else None, numero),
                )
                if reativada:
                    conn.execute(
                        """INSERT INTO historico_alteracoes
                           (numeroControlePNCP,data,campo,valorAntigo,valorNovo)
                           VALUES (?,?,'descartada','1','0')""",
                        (numero, dia),
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


def _mesclar_versoes(anteriores, novos):
    if anteriores and anteriores.get("dataAtualizacao") != novos.get("dataAtualizacao"):
        data_anterior = dados_mod._timestamp_brasilia(anteriores.get("dataAtualizacao"))
        data_nova = dados_mod._timestamp_brasilia(novos.get("dataAtualizacao"))
        if data_nova < data_anterior:
            return _mesclar_dados(novos, anteriores)
        if pd.notna(data_anterior) and pd.isna(data_nova):
            novos = {**novos, "dataAtualizacao": anteriores["dataAtualizacao"]}
            if anteriores.get("situacaoCompraNome"):
                novos["situacaoCompraNome"] = anteriores["situacaoCompraNome"]
    return _mesclar_dados(anteriores, novos)


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
        if origem == "valorTotalEstimado" and dados.get(origem) == 0:
            continue
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
    return _mesclar_versoes(dados, item)


def salvar_licitacao(
    conn, item, classificacao, atual=None, rodada_id=None, config=None, dia_coleta=None, descartar=False
):
    config = configuracao_mod._configuracao(config)
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
    data_coleta = dia_coleta or configuracao_mod.hoje_brasilia()
    hoje = data_coleta.isoformat()
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
                if valor_novo is None:
                    valor_novo = valor_antigo
            elif valor_novo is None and valor_antigo is not None:
                valor_novo = valor_antigo
            if valor_novo != valor_antigo:
                alteracoes.append((campo, valor_antigo, valor_novo))
        campo = "informacaoComplementar"
        anterior = _item_mesclado(atual, {}).get(campo)
        atualizada = item.get(campo)
        if str(anterior or "").strip() != str(atualizada or "").strip():
            alteracoes.append((campo, anterior, atualizada))
        if atual.get("descartada") and not descartar:
            alteracoes.append(("descartada", 1, 0))
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
            linha = f"{data_coleta:%d/%m/%Y}: " + "; ".join(trechos)
            resumo = (resumo + " | " + linha).strip(" |")
    else:
        resumo = None
    final = dict(novo)
    if atual:
        for campo, valor_novo in final.items():
            if valor_novo is None and atual.get(campo) is not None:
                final[campo] = atual[campo]
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
    conn, escaneados, encontradas, falhas, interrompido, estado=None, rodada_id=None,
    dia_coleta=None, chamadas=None, pendentes=None, tentativa_id=None,
):
    estado = estado or (
        "completa" if not falhas and not interrompido else "parcial" if escaneados else "falhou"
    )
    conn.execute(
        """INSERT INTO log_execucoes
           (rowid,timestamp,total_escaneados,total_encontradas,total_falhas,interrompido_por_circuito,
            estado,rodada_id,dia_coleta,total_chamadas,combinacoes_pendentes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(rowid) DO UPDATE SET timestamp=excluded.timestamp,
               total_escaneados=excluded.total_escaneados, total_encontradas=excluded.total_encontradas,
               total_falhas=excluded.total_falhas, interrompido_por_circuito=excluded.interrompido_por_circuito,
               estado=excluded.estado, total_chamadas=excluded.total_chamadas,
               combinacoes_pendentes=excluded.combinacoes_pendentes""",
        (
            tentativa_id,
            configuracao_mod.agora_brasilia().isoformat(timespec="seconds"),
            escaneados,
            encontradas,
            falhas,
            int(interrompido),
            estado,
            rodada_id,
            dia_coleta.isoformat() if dia_coleta else None,
            chamadas,
            pendentes,
        ),
    )


def _rodada_do_dia(conn, dia):
    rodada = conn.execute(
        """SELECT rodada_id,assinatura,estado FROM log_execucoes
            WHERE dia_coleta=? AND assinatura IS NOT NULL ORDER BY rowid DESC LIMIT 1""",
        (dia,),
    ).fetchone()
    if rodada:
        return rodada
    # Bancos anteriores ao registro no início da rodada dependiam dos checkpoints.
    checkpoints = conn.execute(
        """SELECT rodada_id,assinatura FROM progresso WHERE dia=?
           UNION SELECT rodada_id,assinatura FROM paginas_coleta WHERE dia=?""",
        (dia, dia),
    ).fetchall()
    if not checkpoints:
        return None, None, None
    if len(checkpoints) != 1 or not all(checkpoints[0]):
        return None, None, "incompatível"
    rodada_id, assinatura = checkpoints[0]
    registro = conn.execute(
        "SELECT estado FROM log_execucoes WHERE rodada_id=? ORDER BY rowid DESC LIMIT 1",
        (rodada_id,),
    ).fetchone()
    return rodada_id, assinatura, (registro and registro[0]) or "em_andamento"


def _salvar_descartada(
    conn, item, motivo, detalhes=None, atual=None, rodada_id=None, config=None, dia_coleta=None
):
    detalhes = detalhes or {"score": 0, "termos": [], "principal": ""}
    salvar_licitacao(
        conn,
        item,
        (detalhes["score"], detalhes["termos"], detalhes["principal"]),
        atual=atual,
        rodada_id=rodada_id,
        config=config,
        dia_coleta=dia_coleta,
        descartar=True,
    )
    conn.execute(
        "UPDATE licitacoes SET descartada=1,motivoDescarte=? WHERE numeroControlePNCP=?",
        (motivo, item.get("numeroControlePNCP")),
    )


def _estado_marcacao(conn, numero):
    return conn.execute(
        """SELECT COALESCE(jaCaptou,0),COALESCE((SELECT MAX(id) FROM historico_marcacoes h
                      WHERE h.numeroControlePNCP=licitacoes.numeroControlePNCP),0)
             FROM licitacoes WHERE numeroControlePNCP=?""",
        (numero,),
    ).fetchone()


def _registrar_marcacao(conn, numero, anterior, valor, revisao, origem, reverte_id=None, motivo=None):
    motivo_antigo = conn.execute(
        "SELECT motivoDesinteresse FROM licitacoes WHERE numeroControlePNCP=?", (numero,)
    ).fetchone()
    motivo_antigo = motivo_antigo[0] if motivo_antigo else ""
    motivo = (motivo_antigo if motivo is None else motivo) if valor == 2 else ""
    if motivo and motivo not in configuracao_mod.MOTIVOS_DESINTERESSE:
        raise ValueError("Motivo do desinteresse inválido")
    cur = conn.execute(
        """UPDATE licitacoes SET jaCaptou=?,motivoDesinteresse=? WHERE numeroControlePNCP=? AND COALESCE(jaCaptou,0)=?
               AND COALESCE((SELECT MAX(id) FROM historico_marcacoes
                             WHERE numeroControlePNCP=?),0)=?""",
        (valor, motivo, numero, anterior, numero, revisao),
    )
    if cur.rowcount != 1:
        raise ValueError(f"Conflito de edição para {numero}")
    conn.execute(
        """INSERT INTO historico_marcacoes
           (numeroControlePNCP,data,valorAntigo,valorNovo,origem,reverte_id,motivoAntigo,motivoNovo)
           VALUES (?,?,?,?,?,?,?,?)""",
        (numero, configuracao_mod.agora_brasilia().isoformat(timespec="seconds"), anterior, valor,
         origem, reverte_id, motivo_antigo, motivo),
    )


def restaurar_marcacao(conn, alteracao_id):
    alteracao = conn.execute(
        "SELECT numeroControlePNCP,valorAntigo,valorNovo,motivoAntigo FROM historico_marcacoes WHERE id=?",
        (alteracao_id,),
    ).fetchone()
    if alteracao is None:
        raise ValueError(f"Alteração de marcação {alteracao_id} não encontrada")
    numero, anterior, valor, motivo = alteracao
    if _estado_marcacao(conn, numero) != (valor, alteracao_id):
        raise ValueError(f"O edital {numero} tem outra marcação desde a alteração {alteracao_id}")
    with conn:
        _registrar_marcacao(conn, numero, valor, anterior, alteracao_id, "Restauração", alteracao_id, motivo=motivo)
    return numero


def _ler_ultima_coleta(conn):
    linha = conn.execute(
        """SELECT COALESCE(estado, CASE WHEN COALESCE(total_falhas,0)=0
                                         AND COALESCE(interrompido_por_circuito,0)=0
                                    THEN 'completa' ELSE 'falhou' END),
                  COALESCE(dia_coleta,substr(timestamp,1,10)), rodada_id, total_chamadas,
                  total_escaneados, combinacoes_pendentes, total_falhas, rowid, total_combinacoes
             FROM log_execucoes ORDER BY rowid DESC LIMIT 1"""
    ).fetchone()
    if linha is None or linha[-1] is None:
        dia_checkpoint = conn.execute(
            """SELECT MAX(dia) FROM (
                   SELECT dia FROM progresso WHERE rodada_id IS NOT NULL
                   UNION ALL SELECT dia FROM paginas_coleta WHERE rodada_id IS NOT NULL
               )"""
        ).fetchone()[0]
        if dia_checkpoint and (linha is None or dia_checkpoint >= linha[1]):
            rodada, _, estado = _rodada_do_dia(conn, dia_checkpoint)
            if estado in ("em_andamento", "incompatível") and (linha is None or rodada != linha[2]):
                linha = ("em_andamento", dia_checkpoint, rodada, None, None, None, None, None, None)
    if linha is None:
        return None
    estado, dia, rodada, chamadas, escaneados, pendentes, falhas, tentativa, total = linha
    if estado == "em_andamento" and total is not None:
        concluidas = conn.execute(
            "SELECT COUNT(*) FROM progresso WHERE dia=? AND rodada_id=?", (dia, rodada)
        ).fetchone()[0]
        pendentes = total - concluidas
    return {
        "estado": "completa" if estado == "completa" else "parcial",
        "situacao_tentativa": "interrompida" if estado == "em_andamento" else estado,
        "dia": datetime.fromisoformat(dia).date(), "tentativa_id": tentativa,
        "rodada_id": rodada, "chamadas": chamadas, "escaneados": escaneados,
        "pendentes": pendentes, "falhas": falhas, "total_combinacoes": total,
    }


def ultima_coleta_salva():
    if not configuracao_mod.DB_PATH.is_file():
        raise ValueError("Banco do Radar ausente")
    with _bloquear_banco(), closing(iniciar_banco()) as conn:
        coleta = _ler_ultima_coleta(conn)
    if coleta is None:
        raise ValueError("Nenhuma coleta completa ou parcial para exportar")
    return coleta
