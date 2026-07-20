"""
Servidor MCP de finanzas personales de Eduardo.

Expone herramientas para CONSULTAR y REGISTRAR movimientos en un Google Sheet
con tres hojas: 'gastos', 'ingresos', 'transferencias'.

Requisitos (instalar una vez):
    pip install "mcp[cli]" gspread google-auth uvicorn starlette

Funciona en dos modos:

  LOCAL (Claude Desktop, sin auth):
      No definir TRANSPORT. Corre por stdio.
      GOOGLE_CREDS_JSON = ruta al archivo .json de credenciales.

  REMOTO / HOSTEADO (Claude web/Desktop vía conector personalizado):
      TRANSPORT=http
      GOOGLE_CREDS_JSON = CONTENIDO del json (pegado como variable de entorno)
      MCP_API_KEY = un token secreto largo que tú inventas (protege el acceso)
      PORT = lo asigna el host (Railway/Render); por defecto 8000
"""

import json
import os
from datetime import date

import gspread
from google.oauth2.service_account import Credentials
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Conexión al Google Sheet
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NOMBRE = os.environ.get("SHEET_NOMBRE", "Finanzas Eduardo")

# Las credenciales pueden venir como RUTA a un archivo (local) o como el
# CONTENIDO del JSON pegado en la variable de entorno (hosting).
_creds_raw = os.environ["GOOGLE_CREDS_JSON"]
if os.path.exists(_creds_raw):
    creds = Credentials.from_service_account_file(_creds_raw, scopes=SCOPES)
else:
    creds = Credentials.from_service_account_info(json.loads(_creds_raw), scopes=SCOPES)

gc = gspread.authorize(creds)
libro = gc.open(SHEET_NOMBRE)

# Categorías válidas (deben coincidir con el bundle OKF)
CATS_GASTO = {
    "Salidas", "Compras", "Comida", "Compras mías", "Transporte", "Rutina",
    "Viaje", "Regalos", "Ocio", "Educación", "Megas", "Otros", "Date Camila",
    "Claude", "Salud", "Corte de pelo",
}
CATS_INGRESO = {
    "Mesada", "WT", "Trabajo", "Regalo", "Otros", "Pasantía", "Clases", "Rhh",
}

# Umbrales de confirmación para evitar errores de tipeo
LIMITE_GASTO = 200.0
LIMITE_INGRESO = 1000.0

# Cuentas de deuda ya saldadas: se conservan en el histórico pero no se usan
# para movimientos nuevos (ver cuentas.md en el bundle OKF).
CUENTAS_CERRADAS = {"Juanse", "Lucas", "Moses"}

mcp = FastMCP("finanzas-eduardo")


def _filas(hoja: str) -> list[dict]:
    return libro.worksheet(hoja).get_all_records()


# ---------------------------------------------------------------------------
# HERRAMIENTAS DE LECTURA
# ---------------------------------------------------------------------------
@mcp.tool()
def gastos_del_mes(mes: str) -> str:
    """Suma total de gastos de un mes. 'mes' en formato AAAA-MM (ej. 2026-07).
    Excluye transferencias. Devuelve el total y el desglose por categoría."""
    filas = [f for f in _filas("gastos") if str(f["fecha"]).startswith(mes)]
    if not filas:
        return f"No hay gastos registrados en {mes}."
    total = sum(float(f["monto"]) for f in filas)
    por_cat: dict[str, float] = {}
    for f in filas:
        por_cat[f["categoria"]] = por_cat.get(f["categoria"], 0) + float(f["monto"])
    desglose = "\n".join(
        f"  {c}: ${v:,.2f}" for c, v in sorted(por_cat.items(), key=lambda x: -x[1])
    )
    return f"Gastos de {mes}: ${total:,.2f} ({len(filas)} transacciones)\n{desglose}"


@mcp.tool()
def total_categoria(categoria: str, mes: str = "") -> str:
    """Total gastado en una categoría. Si se da 'mes' (AAAA-MM), filtra ese mes;
    si no, suma todo el histórico."""
    filas = [f for f in _filas("gastos") if f["categoria"].lower() == categoria.lower()]
    if mes:
        filas = [f for f in filas if str(f["fecha"]).startswith(mes)]
    if not filas:
        return f"No hay gastos en '{categoria}'" + (f" en {mes}." if mes else ".")
    total = sum(float(f["monto"]) for f in filas)
    periodo = mes if mes else "todo el histórico"
    return f"'{categoria}' en {periodo}: ${total:,.2f} ({len(filas)} transacciones)"


@mcp.tool()
def buscar_gastos(texto: str) -> str:
    """Busca gastos cuyo comentario contenga 'texto' (ej. 'Uber', 'Katari').
    Útil para rastrear en qué se fue el dinero en algo específico."""
    filas = [
        f for f in _filas("gastos")
        if texto.lower() in str(f.get("comentario", "")).lower()
    ]
    if not filas:
        return f"No se encontraron gastos con '{texto}' en el comentario."
    total = sum(float(f["monto"]) for f in filas)
    ejemplos = "\n".join(
        f"  {f['fecha']} · {f['categoria']} · ${float(f['monto']):,.2f} · {f['comentario']}"
        for f in filas[:15]
    )
    extra = f"\n  ... y {len(filas) - 15} más" if len(filas) > 15 else ""
    return f"'{texto}': {len(filas)} gastos, ${total:,.2f} en total\n{ejemplos}{extra}"


@mcp.tool()
def balance_del_mes(mes: str) -> str:
    """Balance de un mes: ingresos menos gastos (AAAA-MM). Excluye transferencias."""
    g = sum(float(f["monto"]) for f in _filas("gastos") if str(f["fecha"]).startswith(mes))
    i = sum(float(f["monto"]) for f in _filas("ingresos") if str(f["fecha"]).startswith(mes))
    neto = i - g
    signo = "ahorro" if neto >= 0 else "déficit"
    return (f"Balance de {mes}:\n  Ingresos: ${i:,.2f}\n  Gastos: ${g:,.2f}\n"
            f"  Neto: ${neto:,.2f} ({signo})")


# ---------------------------------------------------------------------------
# HERRAMIENTAS DE ESCRITURA
# ---------------------------------------------------------------------------
@mcp.tool()
def registrar_gasto(monto: float, categoria: str, cuenta: str,
                    comentario: str = "", confirmado: bool = False) -> str:
    """Registra un gasto nuevo. Valida la categoría. Si el monto supera $200,
    exige confirmado=True antes de escribir (protección contra errores de tipeo)."""
    if categoria not in CATS_GASTO:
        return (f"Categoría '{categoria}' no válida. Opciones: "
                f"{', '.join(sorted(CATS_GASTO))}. No se registró nada.")
    if cuenta in CUENTAS_CERRADAS:
        return (f"'{cuenta}' es una cuenta de deuda cerrada y no se usa para "
                f"movimientos nuevos. No se registró nada.")
    if monto > LIMITE_GASTO and not confirmado:
        return (f"⚠️ Gasto de ${monto:,.2f} en '{categoria}' supera ${LIMITE_GASTO:.0f}. "
                f"Confirma que el monto es correcto y vuelve a llamar con confirmado=True.")
    fila = [str(date.today()), categoria, cuenta, round(monto, 2), comentario]
    libro.worksheet("gastos").append_row(fila)
    return f"✓ Gasto registrado: ${monto:,.2f} en {categoria} ({cuenta}) — {comentario}"


@mcp.tool()
def registrar_ingreso(monto: float, categoria: str, cuenta: str,
                     comentario: str = "", confirmado: bool = False) -> str:
    """Registra un ingreso nuevo. Valida la categoría. Si el monto supera $1000,
    exige confirmado=True antes de escribir."""
    if categoria not in CATS_INGRESO:
        return (f"Categoría '{categoria}' no válida. Opciones: "
                f"{', '.join(sorted(CATS_INGRESO))}. No se registró nada.")
    if cuenta in CUENTAS_CERRADAS:
        return (f"'{cuenta}' es una cuenta de deuda cerrada y no se usa para "
                f"movimientos nuevos. No se registró nada.")
    if monto > LIMITE_INGRESO and not confirmado:
        return (f"⚠️ Ingreso de ${monto:,.2f} en '{categoria}' supera ${LIMITE_INGRESO:.0f}. "
                f"Confirma que el monto es correcto y vuelve a llamar con confirmado=True.")
    fila = [str(date.today()), categoria, cuenta, round(monto, 2), comentario]
    libro.worksheet("ingresos").append_row(fila)
    return f"✓ Ingreso registrado: ${monto:,.2f} en {categoria} ({cuenta}) — {comentario}"


@mcp.tool()
def registrar_transferencia(origen: str, destino: str, monto: float,
                           comentario: str = "") -> str:
    """Registra una transferencia entre cuentas propias. No cuenta como gasto ni ingreso."""
    cerradas = CUENTAS_CERRADAS & {origen, destino}
    if cerradas:
        return (f"{', '.join(cerradas)}: cuenta(s) de deuda cerrada(s), no se usan "
                f"para movimientos nuevos. No se registró nada.")
    fila = [str(date.today()), origen, destino, round(monto, 2), comentario]
    libro.worksheet("transferencias").append_row(fila)
    return f"✓ Transferencia registrada: ${monto:,.2f} de {origen} a {destino}"


if __name__ == "__main__":
    if os.environ.get("TRANSPORT") == "http":
        # --- Modo remoto/hosteado: HTTP con autenticación por token ---
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        API_KEY = os.environ["MCP_API_KEY"]

        class AuthMiddleware(BaseHTTPMiddleware):
            """Exige el token secreto en cada petición. Sin él, 401.
            Claude lo envía como header x-api-key (o Authorization: Bearer ...)."""
            async def dispatch(self, request, call_next):
                enviado = request.headers.get("x-api-key")
                if not enviado:
                    auth = request.headers.get("authorization", "")
                    enviado = auth[7:] if auth.lower().startswith("bearer ") else auth
                if enviado != API_KEY:
                    return JSONResponse({"error": "no autorizado"}, status_code=401)
                return await call_next(request)

        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        app = mcp.streamable_http_app()
        app.add_middleware(AuthMiddleware)
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
    else:
        # --- Modo local: stdio para Claude Desktop ---
        mcp.run()
