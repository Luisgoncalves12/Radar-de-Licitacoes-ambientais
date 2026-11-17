"""Regras de selecao e assinatura da classificacao."""

from functools import lru_cache
import hashlib
import json
import re
import unicodedata

from . import configuracao as configuracao_mod


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
_NOME_ADMINISTRATIVO = re.compile(
    r"\b(?:secretarias?|departamentos?|diretorias?|superintendencias?)\b"
    r"(?:(?![.;:\n]|\b(?:contrat\w*|prestacao|servicos?|execucao|elaboracao|para|incluindo|visando|compreendendo)\b).){0,160}?"
    r"\b(?P<termo>gestao ambiental|gerenciamento ambiental)\b"
)


def _sem_termos_administrativos(texto):
    """Ignora a área administrativa sem apagar outros serviços da mesma frase."""
    return _NOME_ADMINISTRATIVO.sub(
        lambda m: m.group()[:m.start("termo") - m.start()] + " " * len(m.group("termo")),
        texto,
    )


_MENCOES_INCIDENTAIS = re.compile(
    r"\b(?P<norma>eia)(?=\s*[/ -]?\s*tia\b)"
    r"|\b(?:rio das|profissionais da|usuarios da)\s+(?P<ras_local>ras)\b"
    r"|\b(?P<ras_medico>ras)\s*(?:[-–]\s*)?(?:registro de atendimento|do sus|da saude)\b"
    r"|\b(?P<ras_regiao>ras)(?=\s*[-–]\s*ap\s*\d)"
    r"|\brede de atencao a saude\s*\(?\s*(?P<ras_rede>ras)\b"
    r"|\bmunicipio de\s+(?P<prad_local>prad)\b"
    r"|\bmuseu de\s+(?P<museu>arqueologia)\b"
    r"|\bcentral de\s+(?P<empresa>gerenciamento ambiental)\b"
    r"|\b(?:portando|apresentar o)\s+(?P<licenca>licenciamento ambiental)\b"
    r"|\b(?:coleta de|materiais de|residuos de)\s+(?P<poda_residuo>podas?(?: de arvores)?)\b"
    r"|\b(?:residuos|entulhos)\b"
    r"(?:(?![.;:]|\b(?:execucao|prestacao|servicos?|poda e|manejo|capina|capinagem|rocada|jardinagem|varricao|extracao|plantio)\b).){0,180}?"
    r"\b(?P<poda_lista>podas?(?: de arvores)?)\b"
)
_SERVICO_CONTRATADO = re.compile(
    r"\b(?:prestacao|execucao|elaboracao|construcao|implantacao|executar)\b"
    r"|\bcontratacao\s+de\s+servicos?\b|^\s*servicos?\b"
)
_COMPRA_EXPLICITA = re.compile(
    r"\baquisicao(?:\s+(?:parcelada|futura|eventual))?\s+de\b"
    r"|\bfornecimento(?:\s+parcelado)?\s+de\s+"
    r"(?:bens|equipamentos?|materia(?:l|is)|sementes|mudas|oleo|ferramentas)\b"
)


def _sem_mencoes_incidentais(texto):
    def apagar(m):
        inicio, fim = m.span(m.lastgroup)
        return m.group()[:inicio - m.start()] + " " * (fim - inicio) + m.group()[fim - m.start():]
    return _MENCOES_INCIDENTAIS.sub(apagar, texto)


def _motivo_objeto_alheio(texto, termos):
    """Exclusões estreitas para objetos revisados; contratos mistos ficam na triagem."""
    software = re.search(
        r"\b(?:locacao|fornecimento|licenca de uso|licenciamento de uso|implantacao|"
        r"manutencao|suporte|servicos de sistema)\b.{0,160}?"
        r"\b(?:software|plataforma informatizada|sistema integrado|ferramenta tecnologica)\b",
        texto[:650],
    )
    if software and not re.search(
        r"\b(?:elaboracao de estudos ambientais|execucao de manejo arboreo|supervisao ambiental)\b",
        texto[:software.start()],
    ):
        return "solução de software, não execução de serviço ambiental"
    if re.match(r"\s*(?:servicos de sistema|sistema integrado)\b", texto) and re.search(r"\bsaas\b", texto):
        return "solução de software, não execução de serviço ambiental"
    if re.search(r"\bcontratacao de solucao informatizada\b", texto[:200]):
        return "solução de software, não execução de serviço ambiental"
    if re.match(r"\s*passagens aereas\b", texto):
        return "passagens para evento, não serviço ambiental"
    if re.match(r"\s*(?:ancinho|tesoura|pulverizador|triturador de galhos|palete de madeira|bombas hidraulicas|materiais permanentes)\b", texto) and not _SERVICO_CONTRATADO.search(texto):
        return "fornecimento de bens com menção ambiental incidental"
    if set(termos) <= {"monitoramento ambiental"} and re.search(r"\bcalibracao\b", texto) and re.search(r"\b(?:bioterios?|ensaios clinicos)\b", texto):
        return "calibração de instrumentos clínicos, não monitoramento ambiental"
    if set(termos) <= {"fauna silvestre"} and re.search(r"\bcoffee break\b", texto):
        return "alimentação para evento, não serviço de fauna"
    if set(termos) <= {"poda"} and re.search(
        r"\b(?:manutencao em|manutencao.{0,70}equipamentos motorizados)\b", texto
    ) and re.search(r"\b(?:rocadeiras?|moto serras?|motosserras?|moto podas?)\b", texto):
        return "manutenção de ferramentas, não execução de poda"
    if set(termos) <= {"programas ambientais", "educação ambiental"} and re.search(
        r"\b(?:medicina ocupacional|pcmso|ltcat|saude ocupacional)\b", texto
    ):
        return "saúde ocupacional, não programa ambiental do Radar"
    if set(termos) <= {"arqueologia"} and re.search(r"\b(?:bolsas para estagiarios|recrutamento)\b", texto):
        return "recrutamento de estagiários, não serviço arqueológico"
    return ""


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
    extras = tuple(configuracao_mod.VARIANTES_TERMOS.get(termo, ())) if variantes else ()
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
    return (str(uf or "").strip().upper(), _normalizar(municipio).strip()) in configuracao_mod.LOCAIS_BLOQUEADOS


def _texto_para_classificar(objeto_compra, informacao_complementar=None):
    """Reúne objeto e informação complementar sem repetir o mesmo texto."""
    partes = []
    for valor in (objeto_compra, informacao_complementar):
        texto = str(valor or "").strip()
        if texto and texto not in partes:
            partes.append(texto)
    return " ".join(partes)


def _tem_familia_topografia(texto, config=None):
    config = configuracao_mod._configuracao(config)
    return any(_contem(texto, termo) for termo in config["topografia_familia"])


def classificar(objeto_compra, detalhado=False, orgao=None, _usar_variantes=False, config=None):
    config = configuracao_mod._configuracao(config)
    termos = config["termos"]
    familias = config["topografia_familia"]
    score_minimo = config["score_minimo"]
    regex_exclusao, regex_resgate = _filtros_compilados(
        tuple(config["exclusoes"]), tuple(config["resgate"])
    )
    texto = str(objeto_compra or "")
    normalizado = _normalizar(texto)
    texto_servico = _sem_mencoes_incidentais(_sem_termos_administrativos(normalizado))
    contexto_orgao = bool(
        re.search(
            r"\b(?:dnit|der|daer|sinfra)\b|departamento nacional de infraestrutura de transportes",
            _normalizar(orgao or ""),
        )
    )
    contexto_infra = contexto_orgao or bool(_CONTEXTO_INFRA.search(normalizado))
    contexto_manejo = bool(_CONTEXTO_PLANO_MAN.search(normalizado))
    tradicionais, ampliados, familia, termos_topograficos = _preparar_padroes(
        tuple(termos), tuple(familias), tuple((t, tuple(v)) for t, v in configuracao_mod.VARIANTES_TERMOS.items())
    )
    tem_topografia = any(p.search(normalizado) for p in familia)
    modos = (ampliados,) if _usar_variantes else (tradicionais, ampliados)
    for padroes in modos:
        achados = [
            t
            for t, padrao in padroes
            if padrao.search(texto_servico)
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
        compra_explicita = _COMPRA_EXPLICITA.search(normalizado)
        servico_contratado = _SERVICO_CONTRATADO.search(normalizado)
        servico_antes_da_compra = servico_contratado and (
            not compra_explicita or servico_contratado.start() < compra_explicita.start()
        )
        servico_integrado = re.search(
            r"\b(?:com|incluindo|e)\s+(?:a\s+)?(?:prestacao|execucao)\s+de\s+servicos?\b",
            normalizado,
        )
        tem_servico = regex_resgate.search(texto) or re.search(
            r"\b(?:prestacao de servicos?|instalacao de|incluindo instalacao)\b", normalizado
        )
        if (compra_explicita and not (servico_antes_da_compra or servico_integrado)) or (
            not compra_explicita and not tem_forte and regex_exclusao.search(texto) and not tem_servico
        ):
            manter = False
            if not motivo:
                motivo = "compra de bens sem termo forte/serviço"
        motivo_alheio = _motivo_objeto_alheio(normalizado, termos_score)
        if manter and motivo_alheio:
            manter, motivo = False, motivo_alheio
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


def assinatura_classificacao(config=None):
    config = configuracao_mod._configuracao(config)
    dados = {
        "termos": config["termos"],
        "score_minimo": config["score_minimo"],
        "exclusoes": config["exclusoes"],
        "resgate": config["resgate"],
        "familia": config["topografia_familia"],
        "variantes": configuracao_mod.VARIANTES_TERMOS,
        "contextuais": sorted(TERMOS_CONTEXTUAIS),
        "infra": _CONTEXTO_INFRA.pattern,
        "manejo": _CONTEXTO_PLANO_MAN.pattern,
        "versao": configuracao_mod.VERSAO_REGRAS,
    }
    return hashlib.sha256(
        json.dumps(dados, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
