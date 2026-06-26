"""
report.py
Genera un reporte en Markdown + graficos PNG con todos los hallazgos.
"""
import os

import matplotlib
matplotlib.use("Agg")  # backend sin pantalla
import matplotlib.pyplot as plt
import pandas as pd

import config

MESES = ["", "Ene", "Feb", "Mar", "Abr", "May", "Jun",
         "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
DIAS = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"]


def _chart_price(name: str, df: pd.DataFrame) -> str:
    path = os.path.join(config.OUTPUT_DIR, f"{name}_precio.png")
    plt.figure(figsize=(10, 4))
    plt.plot(df.index, df["Close"], label="Cierre", linewidth=1)
    if df["SMA_FAST"].notna().any():
        plt.plot(df.index, df["SMA_FAST"], label=f"SMA{config.SMA_FAST}", linewidth=0.9)
    if df["SMA_SLOW"].notna().any():
        plt.plot(df.index, df["SMA_SLOW"], label=f"SMA{config.SMA_SLOW}", linewidth=0.9)
    plt.title(f"{name} - Precio y medias")
    plt.legend(fontsize=8)
    plt.tight_layout()
    plt.savefig(path, dpi=110)
    plt.close()
    return path


def _chart_seasonality(name: str, by_month: pd.DataFrame) -> str:
    path = os.path.join(config.OUTPUT_DIR, f"{name}_estacionalidad.png")
    plt.figure(figsize=(10, 4))
    means = by_month["mean"] * 100
    plt.bar([MESES[m] for m in means.index], means.values)
    plt.axhline(0, color="black", linewidth=0.6)
    plt.title(f"{name} - Retorno diario medio por mes (%)")
    plt.tight_layout()
    plt.savefig(path, dpi=110)
    plt.close()
    return path


def _md_table(df: pd.DataFrame, floatfmt="{:.4f}") -> str:
    if df is None or len(df) == 0:
        return "_Sin datos._\n"
    cols = list(df.columns)
    head = "| " + " | ".join(str(c) for c in cols) + " |\n"
    sep = "| " + " | ".join("---" for _ in cols) + " |\n"
    body = ""
    for _, row in df.iterrows():
        cells = []
        for c in cols:
            v = row[c]
            cells.append(floatfmt.format(v) if isinstance(v, float) else str(v))
        body += "| " + " | ".join(cells) + " |\n"
    return head + sep + body


def build_report(results: dict, ai_text: str) -> str:
    """results: estructura producida por main.run(). Devuelve la ruta del .md"""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    lines = []
    lines.append("# Reporte de Patrones de Mercado\n")
    lines.append(f"Ventana: **{config.START_DATE} -> {config.END_DATE}** "
                 f"({config.YEARS_BACK} anios)\n")

    # Correlacion
    lines.append("## Correlacion de retornos (contexto)\n")
    lines.append(_md_table(results["correlation"].reset_index().rename(
        columns={"index": "activo"})))

    for name, r in results["assets"].items():
        lines.append(f"\n---\n\n# {name}\n")

        _chart_price(name, r["df"])
        lines.append(f"![precio]({name}_precio.png)\n")

        # Estacionalidad
        lines.append("## Estacionalidad / ciclos\n")
        seas = r["seasonality"]
        lines.append(f"- Mejor mes historico: **{MESES[seas['best_month']]}** | "
                     f"Peor mes: **{MESES[seas['worst_month']]}**\n")
        _chart_seasonality(name, seas["by_month"])
        lines.append(f"![estacionalidad]({name}_estacionalidad.png)\n")
        bm = seas["by_month"].copy()
        bm.index = [MESES[m] for m in bm.index]
        lines.append(_md_table(bm.reset_index().rename(columns={"index": "mes"})))

        # Setups tecnicos
        lines.append("\n## Setups tecnicos (backtest)\n")
        lines.append("_`significativo` = p<0.05 y n>=30 (ventaja distinguible "
                     "del azar, no garantia futura)._\n")
        lines.append(_md_table(r["signals_eval"]))

        # Validacion walk-forward
        lines.append("\n## Validacion walk-forward (in-sample vs out-of-sample)\n")
        wf = r.get("signals_wf")
        if wf is not None and len(wf) > 0:
            lines.append("_`consistente` = el signo del retorno medio se mantiene "
                         "en el tramo fuera de muestra (indicio de edge real)._\n")
            lines.append(_md_table(wf))
        else:
            lines.append("_Muestra insuficiente para particionar._\n")

        # Eventos macro
        lines.append("\n## Reaccion a eventos macro\n")
        by_type = r.get("event_study_by_type") or {}
        if by_type:
            for ev_type, study in by_type.items():
                lines.append(f"\n### {ev_type}\n")
                lines.append(_md_table(study))
        elif r["event_study"] is not None and len(r["event_study"]) > 0:
            lines.append(_md_table(r["event_study"]))
        else:
            lines.append("_No se cargaron eventos en rango (revisa events.csv)._\n")

    # Interpretacion IA
    lines.append("\n---\n\n# Interpretacion (capa IA)\n")
    lines.append(ai_text + "\n")
    lines.append("\n> Aviso: este reporte es analitico/educativo y NO constituye "
                 "asesoria de inversion.\n")

    path = os.path.join(config.OUTPUT_DIR, "reporte.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
