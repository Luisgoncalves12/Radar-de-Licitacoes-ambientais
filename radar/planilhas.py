"""Importacao de marcacoes e exportacao das planilhas Excel."""

from contextlib import closing
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zipfile import BadZipFile
import json
import os
import shutil
import tempfile

import pandas as pd

from . import banco as banco_mod
from . import classificacao as classificacao_mod
from . import configuracao as configuracao_mod
from . import dados as dados_mod


def montar_dataframe(
    conn, hoje=None, incluir_sem_interesse=False, config=None, incluir_acompanhamento=False
):
    config = configuracao_mod._configuracao(config)
    rotulos_captacao = {int(k): v for k, v in config["rotulos_captacao"].items()}
    agora_brt = configuracao_mod.agora_brasilia().tz_localize(None)
    hoje = pd.Timestamp(hoje or agora_brt.date()).normalize()
    # O ID da planilha referencia o rowid enquanto o registro permanecer no banco.
    df = pd.read_sql_query(
        """SELECT rowid AS idRadar,numeroControlePNCP,objetoCompra,uf,municipio,
        orgao,modalidadeNome,dataEncerramentoProposta,
        CASE WHEN json_valid(dadosCompletos) THEN
            CASE WHEN json_extract(dadosCompletos,'$.valorTotalEstimado') = 0
                THEN 0 ELSE valorTotalEstimado END
            ELSE valorTotalEstimado END AS valorTotalEstimado,
        dataColeta,primeiraColeta,
        dadosCompletos,situacao,dataAberturaProposta,ultimaAlteracao,descartada,jaCaptou,score,
        palavrasChave,ultimaRodada,dataConsultaSituacao,dataMovimentacaoPNCP,motivoDesinteresse,
        COALESCE((SELECT MAX(id) FROM historico_marcacoes h
                  WHERE h.numeroControlePNCP=licitacoes.numeroControlePNCP),0) AS revisaoMarcacao
        FROM licitacoes""",
        conn,
    )
    locais_bloqueados = pd.Series(
        [classificacao_mod._local_bloqueado(uf, municipio) for uf, municipio in zip(df["uf"], df["municipio"])],
        index=df.index,
        dtype=bool,
    )
    acompanhadas = df["jaCaptou"].eq(1) & incluir_acompanhamento
    df["encerramento"] = pd.to_datetime(
        df["dataEncerramentoProposta"].map(dados_mod._timestamp_brasilia), errors="coerce"
    )
    df["descartada"] = df["descartada"].fillna(0).astype(int)
    df["dnit"] = df.apply(
        lambda r: dados_mod.orgao_preferencial(r.get("orgao"), r.get("dadosCompletos")), axis=1
    )
    df["prioridadeServico"] = [
        dados_mod.prioridade_servico(objeto)
        for objeto in df["objetoCompra"]
    ]
    ainda_aberta = (df["encerramento"] >= agora_brt) | df["encerramento"].isna()
    df["naTriagem"] = ~locais_bloqueados & (df["descartada"] == 0) & ainda_aberta
    df_aberto = df[df["naTriagem"] | acompanhadas].copy()
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
    extras = df_aberto["dadosCompletos"].map(dados_mod._informacoes_apresentacao)
    df_aberto["tipoPublicacao"] = extras.map(lambda valor: valor[0])
    df_aberto["fonteOrcamentaria"] = extras.map(lambda valor: valor[1])
    df_aberto["dias_restantes"] = (df_aberto["encerramento"].dt.normalize() - hoje).dt.days
    excecao_status = df_aberto["situacao"].map(
        lambda valor: dados_mod._situacao_cancelada(valor) or dados_mod._situacao_suspensa(valor)
    )
    sem_prazo = df_aberto["encerramento"].isna()
    df_aberto["naTriagem"] &= (
        (df_aberto["dias_restantes"] >= configuracao_mod.DIAS_MINIMOS) | excecao_status | sem_prazo
    )
    df_aberto = df_aberto[
        df_aberto["naTriagem"] | (df_aberto["jaCaptou"].eq("Sim") & incluir_acompanhamento)
    ].copy()
    abertura = pd.to_datetime(
        df_aberto["dataAberturaProposta"].map(dados_mod._timestamp_brasilia), errors="coerce"
    )
    df_aberto["aAbrir"] = abertura > agora_brt
    df_aberto["prazoIndefinido"] = sem_prazo.loc[df_aberto.index]
    df_aberto["movimento"] = ""
    df_aberto.loc[df_aberto["primeiraColeta"] == hoje.date().isoformat(), "movimento"] = "NOVA"
    df_aberto.loc[
        (df_aberto["movimento"] == "") & (df_aberto["ultimaAlteracao"] == hoje.date().isoformat()),
        "movimento",
    ] = "ATUALIZADA"
    ultima_completa = conn.execute("""SELECT COALESCE(dia_coleta,substr(timestamp,1,10)),rodada_id
        FROM log_execucoes
        WHERE (estado='completa' OR (estado IS NULL AND COALESCE(interrompido_por_circuito,0)=0
        AND COALESCE(total_falhas,0)=0)) ORDER BY timestamp DESC,rowid DESC LIMIT 1""").fetchone()
    df_aberto["cobertura"] = ""
    if ultima_completa:
        data_execucao, ultima_rodada = ultima_completa
        nao_vista = (
            df_aberto["ultimaRodada"].fillna("") != ultima_rodada
            if ultima_rodada else df_aberto["dataColeta"].fillna("") < data_execucao
        )
        df_aberto.loc[nao_vista, "cobertura"] = "FORA DA ÚLTIMA VARREDURA COMPLETA"
    df_aberto["valorTotalEstimado"] = df_aberto["valorTotalEstimado"].astype(object)
    df_aberto.loc[
        df_aberto["valorTotalEstimado"].isna() | (df_aberto["valorTotalEstimado"] == 0),
        "valorTotalEstimado",
    ] = "não informado"
    df_aberto["linkPNCP"] = df_aberto["numeroControlePNCP"].apply(dados_mod.link_pncp)
    df_aberto["urlDestino"] = df_aberto["linkPNCP"]
    df_aberto["abrirEdital"] = df_aberto["urlDestino"].map(
        lambda valor: (
            "Abrir edital"
            if isinstance(valor, str) and valor.startswith(("http://", "https://"))
            else ""
        )
    )
    df_aberto = df_aberto.sort_values(
        ["encerramento", "prioridadeServico", "dnit", "score"],
        ascending=[True, True, False, False], na_position="last", kind="stable"
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
        "cobertura",
        "motivoDesinteresse",
        "aAbrir",
        "prazoIndefinido",
    ]
    df_principal = df_aberto[colunas].copy()
    df_principal = df_principal.rename(columns={"jaCaptou": "Já captei?"})
    df_principal.loc[df_principal["Já captei?"] != "Sem interesse", "motivoDesinteresse"] = ""
    data_cols = ["dataEncerramentoProposta", "dataAberturaProposta", "primeiraColeta", "dataColeta"]
    if incluir_acompanhamento:
        campos = ["dataConsultaSituacao", "dataMovimentacaoPNCP", "ultimaAlteracao"]
        df_principal[campos] = df_aberto[campos]
        df_principal["revisaoMarcacao"] = df_aberto["revisaoMarcacao"]
        df_principal["naTriagem"] = df_aberto["naTriagem"]
        df_principal["fasePrazo"] = "Recebendo propostas"
        df_principal.loc[df_aberto["encerramento"] < agora_brt, "fasePrazo"] = "Prazo encerrado"
        df_principal.loc[df_aberto["aAbrir"], "fasePrazo"] = "A abrir"
        df_principal.loc[df_aberto["prazoIndefinido"], "fasePrazo"] = "Prazo não informado"
        df_principal.loc[excecao_status.loc[df_aberto.index], "fasePrazo"] = "Ver situação"
        data_cols += campos
    for coluna in data_cols:
        df_principal[coluna] = df_principal[coluna].map(dados_mod._timestamp_brasilia)
    for coluna in df_principal.columns:
        if coluna not in data_cols and coluna not in ("aAbrir", "prazoIndefinido", "naTriagem"):
            df_principal[coluna] = df_principal[coluna].map(dados_mod.limpar_para_excel)
    return df_principal


def _baseline_marcacoes(livro):
    try:
        valor = livro.custom_doc_props["RadarMarcacoes"].value
    except KeyError:
        return None
    try:
        dados = json.loads(valor)
        if dados["versao"] not in (1, 2, 3) or not isinstance(dados["linhas"], dict):
            raise ValueError("Versão de marcações inválida")
        return dados["linhas"]
    except (ValueError, TypeError, KeyError) as erro:
        raise ValueError("Metadados de marcações inválidos; importação cancelada") from erro


class PlanilhaIlegivel(ValueError):
    pass


def importar_marcacoes_captacao(conn, caminho, exigir_baseline=False, config=None):
    config = configuracao_mod._configuracao(config)
    status_captacao = config["status_captacao"]
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        livro = load_workbook(caminho, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError, SyntaxError, ValueError) as erro:
        raise PlanilhaIlegivel(f"Não foi possível ler a planilha {caminho}") from erro
    try:
        baseline = _baseline_marcacoes(livro)
        if exigir_baseline and baseline is None:
            configuracao_mod.log.warning(
                "Planilha legada sem referência: use --importar-marcacoes para importar explicitamente"
            )
            return 0
        propostas, estados, referencias = {}, {}, {}
        for nome in (
            "Em Aberto", "DNIT", "Novas", "Atualizadas", "Já captei", "Sem interesse",
            "A abrir", "Revisar prazo", "Acompanhamento",
        ):
            if nome not in livro.sheetnames:
                continue
            aba = livro[nome]
            colunas = {str(c.value or "").strip(): i for i, c in enumerate(aba[1])}
            if "Já captei?" not in colunas:
                continue
            for linha in aba.iter_rows(min_row=2, values_only=True):
                status = classificacao_mod._normalizar(linha[colunas["Já captei?"]]).strip()
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
                    numero = str(linha[colunas["numeroControlePNCP"]] or "").strip()
                if not numero:
                    raise ValueError(f"Número de controle PNCP ausente na aba {nome}")
                if numero not in estados:
                    atual = banco_mod._estado_marcacao(conn, numero)
                    if atual is None:
                        continue
                    estados[numero] = atual
                atual, revisao = estados[numero]
                motivo_atual = conn.execute(
                    "SELECT motivoDesinteresse FROM licitacoes WHERE numeroControlePNCP=?", (numero,)
                ).fetchone()[0]
                anterior = referencia["status"] if referencia else atual
                valor = status_captacao[status]
                motivo_anterior = (
                    referencia.get("motivo", motivo_atual) if referencia else motivo_atual
                ) if anterior == 2 else ""
                motivo = (
                    str(linha[colunas["Motivo do desinteresse"]] or "").strip()
                    if "Motivo do desinteresse" in colunas else motivo_anterior
                ) if valor == 2 else ""
                if motivo and motivo not in configuracao_mod.MOTIVOS_DESINTERESSE:
                    raise ValueError(f"Motivo do desinteresse inválido na aba {nome}")
                if baseline is None and (anterior != 0 or revisao) and valor != anterior:
                    raise ValueError(
                        f"Marcação legada conflita com a decisão salva para {numero}"
                    )
                if baseline is None and motivo != motivo_anterior and (anterior != 0 or revisao):
                    raise ValueError(f"Motivo legado conflita com a decisão salva para {numero}")
                if (valor, motivo) != (anterior, motivo_anterior):
                    propostas.setdefault(numero, set()).add((valor, motivo))
                    referencias[numero] = (anterior, referencia.get("revisao", 0) if referencia else 0)
        plano = []
        for numero, escolhas in propostas.items():
            if len(escolhas) != 1:
                raise ValueError(
                    f"Conflito entre abas para {numero}; nenhuma marcação foi aplicada"
                )
            valor, motivo = next(iter(escolhas))
            atual, revisao = estados[numero]
            motivo_atual = conn.execute(
                "SELECT motivoDesinteresse FROM licitacoes WHERE numeroControlePNCP=?", (numero,)
            ).fetchone()[0]
            if (atual, motivo_atual) == (valor, motivo):
                continue
            if (atual, revisao) != referencias[numero]:
                raise ValueError(f"Conflito com decisão mais recente para {numero}")
            plano.append((numero, atual, valor, revisao, motivo))
    except (BadZipFile, SyntaxError) as erro:
        raise PlanilhaIlegivel(f"Não foi possível ler os dados da planilha {caminho}") from erro
    finally:
        livro.close()
    origem = Path(caminho).name
    with conn:
        for numero, anterior, valor, revisao, motivo in plano:
            banco_mod._registrar_marcacao(conn, numero, anterior, valor, revisao, origem, motivo=motivo)
    return len(plano)


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


def exportar_excel(
    db_path=None, excel_path=None, hoje=None, importar_de=None, config=None, coleta=None
):
    config = configuracao_mod._configuracao(config)
    with banco_mod._bloquear_banco(db_path), closing(banco_mod.iniciar_banco(db_path)) as conn:
        ultima = banco_mod._ler_ultima_coleta(conn)
        if coleta is None and ultima and ultima["rodada_id"]:
            coleta = ultima
        elif coleta is not None and ultima and any(
            chave in coleta and coleta[chave] != ultima[chave]
            for chave in ("tentativa_id", "rodada_id", "dia", "estado")
        ):
            raise ValueError("A rodada solicitada já foi substituída no banco. Use --somente-excel.")
        data_hoje = pd.Timestamp(coleta["dia"] if coleta else hoje or configuracao_mod.hoje_brasilia()).date()
        parcial = coleta is not None and coleta["estado"] == "parcial"
        nome = f"{data_hoje:%d.%m.%Y}{'.parcial' if parcial else ''}.xlsx"
        destino = (
            Path(excel_path) if excel_path else Path(configuracao_mod.PASTA_PLANILHAS) / nome
        )
        destino.parent.mkdir(parents=True, exist_ok=True)
        origem_padrao = (
            destino.with_name(f"{data_hoje:%d.%m.%Y}.xlsx")
            if parcial and not destino.exists() else destino
        )
        planilha_origem = (
            Path(importar_de)
            if importar_de
            else _origem_marcacoes(origem_padrao, data_hoje)
        )
        if planilha_origem:
            try:
                quantidade = importar_marcacoes_captacao(
                    conn, planilha_origem, exigir_baseline=not bool(importar_de), config=config
                )
            except PlanilhaIlegivel:
                if importar_de:
                    raise
                if planilha_origem == destino:
                    copia = destino.with_name(f"{destino.stem}.corrompida-{uuid4().hex}.xlsx")
                    shutil.copy2(destino, copia)
                    configuracao_mod.log.warning("Cópia da planilha ilegível preservada em %s", copia)
                configuracao_mod.log.warning("Marcações de %s indisponíveis; exportando a partir do banco", planilha_origem)
                quantidade = 0
            if quantidade:
                configuracao_mod.log.info(
                    f"Marcações 'Já captei?' importadas de {planilha_origem.name}: {quantidade}"
                )
        conn.commit()
        base = montar_dataframe(
            conn, hoje=data_hoje, incluir_sem_interesse=True, config=config, incluir_acompanhamento=True
        )
        campos_acompanhamento = [
            "fasePrazo", "dataConsultaSituacao", "dataMovimentacaoPNCP", "ultimaAlteracao",
        ]
        acompanhamento = base[base["Já captei?"] == "Sim"].drop(columns=["naTriagem", "cobertura"])
        colunas_inicio = ["dnit", "idVisivel", "Já captei?", "situacao", *campos_acompanhamento, "abrirEdital"]
        acompanhamento = acompanhamento[
            colunas_inicio + [coluna for coluna in acompanhamento if coluna not in colunas_inicio]
        ]
        base = base[base["naTriagem"]].drop(columns=["naTriagem", *campos_acompanhamento])
        df = base[base["Já captei?"] != "Sem interesse"].copy()
        df_sem_interesse = base[base["Já captei?"] == "Sem interesse"].copy()
        df_ja_captei = df[df["Já captei?"] == "Sim"]
        novas = df[df["movimento"] == "NOVA"]
        atualizadas = df[df["movimento"] == "ATUALIZADA"]
        nao_vistas = df[df["cobertura"] != ""]
        dnit = df[df["dnit"] == "DNIT"]
        tabelas = [
            ("Em Aberto", df[~(df["aAbrir"] | df["prazoIndefinido"])]),
            ("DNIT", dnit),
            ("Novas", novas),
            ("Atualizadas", atualizadas),
            ("Já captei", df_ja_captei),
            ("Sem interesse", df_sem_interesse),
            ("A abrir", df[df["aAbrir"] & ~df["prazoIndefinido"]]),
            ("Acompanhamento", acompanhamento),
        ]
        try:
            _gravar_planilha(destino, tabelas, config=config)
        except PermissionError:
            raise PermissionError(
                f"Não foi possível salvar {destino}. Feche o arquivo no Excel e tente novamente."
            )
        configuracao_mod.log.info(
            f"Excel salvo em {destino}: {len(novas)} novas | {len(atualizadas)} atualizadas | "
            f"{len(nao_vistas)} não vistas"
        )
        return destino


def _quadro_historico_marcacoes(conn, config):
    quadro = pd.read_sql_query(
        """SELECT id AS 'Alteração', numeroControlePNCP AS 'Controle PNCP', data AS 'Data',
                  valorAntigo AS 'Antes', valorNovo AS 'Depois', origem AS 'Origem',
                  COALESCE(reverte_id,'') AS 'Reverte a alteração'
             FROM historico_marcacoes ORDER BY id DESC""",
        conn,
    )
    rotulos = {int(k): v for k, v in config["rotulos_captacao"].items()}
    for coluna in ("Antes", "Depois"):
        quadro[coluna] = quadro[coluna].map(rotulos)
    quadro["Origem"] = quadro["Origem"].map(dados_mod.limpar_para_excel)
    return quadro


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
    config = configuracao_mod._configuracao(config)
    status_captacao = config["status_captacao"]
    from openpyxl.packaging.custom import StringProperty
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation

    cores = {"NOVA": "C6EFCE", "ATUALIZADA": "FFEB9C"}
    cor_ja_captei = "DDEBF7"
    sem_interesse_fill = PatternFill(fill_type="solid", fgColor="FFE9DDF5")
    sem_interesse_font = Font(color="FF512E6B")
    rotulos = {
        "dnit": "Prioridade",
        "motivoDesinteresse": "Motivo do desinteresse",
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
        "cobertura": "Cobertura da coleta",
        "fasePrazo": "Prazo das propostas",
        "dataConsultaSituacao": "Situação consultada em",
        "dataMovimentacaoPNCP": "Última atualização no PNCP",
        "ultimaAlteracao": "Última alteração no Radar",
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
                        "status": status_captacao[classificacao_mod._normalizar(status)],
                    }
                if "motivoDesinteresse" in quadro:
                    for identificador, motivo in zip(quadro["idVisivel"], quadro["motivoDesinteresse"]):
                        referencias[identificador]["motivo"] = motivo or ""
                if "revisaoMarcacao" in quadro:
                    for identificador, revisao in zip(quadro["idVisivel"], quadro["revisaoMarcacao"]):
                        referencias[identificador]["revisao"] = int(revisao)
        writer.book.custom_doc_props.append(
            StringProperty(
                name="RadarMarcacoes",
                value=json.dumps({"versao": 3, "linhas": referencias}, ensure_ascii=False),
            )
        )
        for aba_nome, quadro in tabelas:
            colunas_internas = [
                "numeroControlePNCP", "urlDestino", "aAbrir", "prazoIndefinido", "revisaoMarcacao",
            ]
            exportavel = quadro.drop(columns=colunas_internas, errors="ignore")
            if "motivoDesinteresse" in exportavel:
                colunas_visiveis = list(exportavel.columns)
                colunas_visiveis.remove("motivoDesinteresse")
                posicao = colunas_visiveis.index("cobertura") + 1 if "cobertura" in colunas_visiveis else len(colunas_visiveis)
                colunas_visiveis.insert(posicao, "motivoDesinteresse")
                exportavel = exportavel[colunas_visiveis]
            exportavel.rename(columns=rotulos).to_excel(
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
                letra = aba.cell(1, coluna_captacao).column_letter
                faixa_captacao = f"{letra}2:{letra}{aba.max_row}"
                validacao.add(faixa_captacao)
                if "Motivo do desinteresse" in indices:
                    motivo_letra = aba.cell(1, indices["Motivo do desinteresse"]).column_letter
                    faixa_motivo = f"{motivo_letra}2:{motivo_letra}{aba.max_row}"
                    motivos = DataValidation(
                        type="list",
                        formula1='"' + ','.join(configuracao_mod.MOTIVOS_DESINTERESSE) + '"',
                        allow_blank=True,
                    )
                    motivos.showErrorMessage = True
                    motivos.errorTitle = "Motivo inválido"
                    motivos.error = "Escolha um motivo da lista."
                    motivos.showInputMessage = True
                    motivos.promptTitle = "Motivo do desinteresse"
                    motivos.prompt = "Preencha somente para Sem interesse. O motivo será limpo na próxima importação se mudar para Sim ou Não."
                    aba.add_data_validation(motivos)
                    motivos.add(faixa_motivo)
                    aba.conditional_formatting.add(
                        faixa_motivo,
                        FormulaRule(
                            formula=[f'AND(${letra}2="Sem interesse",${motivo_letra}2="")'],
                            fill=PatternFill(fill_type="solid", fgColor="FFF2CC"),
                            stopIfTrue=True,
                        ),
                    )
                marcado = f'${letra}2="Sem interesse"'
                aba.conditional_formatting.add(
                    faixa_captacao,
                    FormulaRule(formula=[marcado], fill=sem_interesse_fill, font=sem_interesse_font),
                )
                condicoes = [marcado]
                if "Situação" in indices:
                    situacao = aba.cell(1, indices["Situação"]).column_letter
                    condicoes.extend(
                        f'TRIM(${situacao}2)<>"{valor}"'
                        for valor in (*dados_mod.SITUACOES_CANCELADAS, *dados_mod.SITUACOES_SUSPENSAS)
                    )
                if "Prioridade" in indices:
                    coluna = indices["Prioridade"]
                    prioridade = aba.cell(1, coluna).column_letter
                    condicoes.append(f'OR(COLUMN()<>{coluna},${prioridade}2<>"DNIT")')
                    condicoes.append(f'OR(COLUMN()<>{coluna},${prioridade}2<>"CODEVASF")')
                ultima_coluna = aba.cell(1, aba.max_column).column_letter
                aba.conditional_formatting.add(
                    f"A2:{ultima_coluna}{aba.max_row}",
                    FormulaRule(
                        formula=[f"AND({','.join(condicoes)})"],
                        fill=sem_interesse_fill,
                        font=sem_interesse_font,
                    ),
                )
            larguras = {
                "Objeto": 70,
                "Órgão": 35,
                "Modalidade": 26,
                "Termos encontrados": 32,
                "Prioridade": 12,
                "Motivo do desinteresse": 36,
                "Atualização": 25,
                "Já captei?": 14,
                "ID": 12,
                "Tipo de publicação": 22,
                "Fonte orçamentária": 48,
                "Abrir edital": 18,
                "Dias restantes": 16,
                "Valor estimado": 18,
                "Prazo das propostas": 22,
                "Situação consultada em": 23,
                "Última atualização no PNCP": 23,
                "Última alteração no Radar": 23,
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
                    "Situação consultada em",
                    "Última atualização no PNCP",
                    "Última alteração no Radar",
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
                    cancelada = dados_mod._situacao_cancelada(situacao)
                    suspensa = dados_mod._situacao_suspensa(situacao)
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
                        and aba.cell(linha, indices["Prioridade"]).value in ("DNIT", "CODEVASF")
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
