"""Indicadores e publicacao do resumo operacional da coleta."""

import os


def resumo_coleta(coleta):
    total = coleta.get("total_combinacoes")
    pendentes = coleta.get("pendentes")
    cobertura = (100 * (total - pendentes) / total
                 if total and pendentes is not None else None)
    return {
        "Estado": coleta["estado"],
        "Operação": coleta.get("operacao"),
        "Dia da coleta": coleta["dia"].isoformat(),
        "Rodada": coleta.get("rodada_id") or "—",
        "Situação da última tentativa": coleta.get("situacao_tentativa") or coleta["estado"],
        "Chamadas na tentativa registrada": coleta.get("chamadas"),
        "Registros escaneados na tentativa registrada": coleta.get("escaneados"),
        "Combinações pendentes": coleta.get("pendentes"),
        "Falhas na tentativa registrada": coleta.get("falhas"),
        "Combinações previstas": total,
        "Cobertura concluída (%)": round(cobertura, 2) if cobertura is not None else None,
        "Meta de cobertura (%)": 95,
        "Meta de cobertura atingida": (
            "Sim" if cobertura >= 95 else "Não"
        ) if cobertura is not None else "Não disponível",
    }


def publicar_resumo(coleta, destino=None):
    resumo = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumo:
        with open(resumo, "a", encoding="utf-8") as arquivo:
            arquivo.write("## Radar PNCP\n\n")
            for indicador, valor in resumo_coleta(coleta).items():
                arquivo.write(f"- **{indicador}** — {'—' if valor is None else valor}\n")
            if destino:
                arquivo.write(f"- **Planilha gerada** — `{destino.name}`\n")
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida and destino:
        with open(saida, "a", encoding="utf-8") as arquivo:
            arquivo.write(f"planilha={destino.name}\n")
