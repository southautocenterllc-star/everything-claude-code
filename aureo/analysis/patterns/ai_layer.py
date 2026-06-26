"""
ai_layer.py
Capa de interpretacion: toma los hallazgos cuantitativos (JSON) y le pide a
Claude un analisis en espaniol, en tono profesional. Si no hay API key o el
paquete no esta instalado, se desactiva sin romper la corrida.
"""
import json
import os

import config


SYSTEM = (
    "Eres un analista cuantitativo senior. Recibes hallazgos ESTADISTICOS ya "
    "calculados (estacionalidad, backtests de senales y estudio de eventos macro) "
    "sobre oro, NASDAQ y S&P 500. Tu trabajo es interpretarlos en espaniol, con "
    "rigor y tono profesional: explica que patrones se repiten, cuales tienen "
    "ventaja estadistica real (ojo a muestras pequenias), y cuales son ruido. "
    "No inventes numeros: usa solo los datos entregados. Cierra con 3 a 5 "
    "observaciones accionables y una advertencia de que esto NO es asesoria de inversion."
)


def interpret(findings: dict) -> str:
    """Devuelve el analisis en texto. Degrada con gracia si falta la config."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ("[Capa IA desactivada] Define la variable de entorno "
                "ANTHROPIC_API_KEY para generar la interpretacion automatica.")
    try:
        import anthropic
    except ImportError:
        return "[Capa IA desactivada] Instala el paquete: pip install anthropic"

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "Estos son los hallazgos cuantitativos en JSON. Interpretalos:\n\n"
        + json.dumps(findings, ensure_ascii=False, indent=2, default=str)
    )

    try:
        msg = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=config.AI_MAX_TOKENS,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    except Exception as e:  # noqa: BLE001
        return f"[Capa IA no disponible] Error al llamar la API: {e}"
