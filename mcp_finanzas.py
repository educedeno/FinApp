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
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Conexión al Google Sheet (perezosa: se conecta al primer uso, no al arrancar,
# para que un error de configuración no tumbe el servidor en bucle)
# ---------------------------------------------------------------------------
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NOMBRE = os.environ.get("SHEET_NOMBRE", "Finanzas Eduardo")

_libro = None


def _conectar():
    raw = os.environ.get("GOOGLE_CREDS_JSON")
    if not raw:
        raise RuntimeError("Falta la variable de entorno GOOGLE_CREDS_JSON.")
    # Puede ser una RUTA a archivo (local) o el CONTENIDO del JSON (hosting).
    if os.path.exists(raw):
        creds = Credentials.from_service_account_file(raw, scopes=SCOPES)
    else:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                "GOOGLE_CREDS_JSON no es un JSON válido. Verifica que pegaste el "
                f"contenido completo del archivo de credenciales. Detalle: {e}"
            )
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    try:
        return gspread.authorize(creds).open(SHEET_NOMBRE)
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"No se encontró un Google Sheet llamado '{SHEET_NOMBRE}'. Revisa que "
            "el nombre en SHEET_NOMBRE coincida EXACTO y que compartiste el Sheet "
            "con el email de la cuenta de servicio (permiso de editor)."
        )


def _get_libro():
    global _libro
    if _libro is None:
        _libro = _conectar()
    return _libro

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

# La protección DNS-rebinding del MCP bloquea por defecto cualquier host que no
# esté en una lista blanca (pensada para servidores locales). Como este servidor
# es remoto y ya está protegido por token, la desactivamos para que acepte el
# dominio de Railway. Si prefieres restringir, define ALLOWED_HOST con tu dominio.
_host_permitido = os.environ.get("ALLOWED_HOST")
if _host_permitido:
    _seguridad = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_host_permitido],
        allowed_origins=[f"https://{_host_permitido}"],
    )
else:
    _seguridad = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP("finanzas-eduardo", transport_security=_seguridad)


def _filas(hoja: str) -> list[dict]:
    return _get_libro().worksheet(hoja).get_all_records()


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
    _get_libro().worksheet("gastos").append_row(fila)
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
    _get_libro().worksheet("ingresos").append_row(fila)
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
    _get_libro().worksheet("transferencias").append_row(fila)
    return f"✓ Transferencia registrada: ${monto:,.2f} de {origen} a {destino}"


if __name__ == "__main__":
    if os.environ.get("TRANSPORT") == "http":
        # --- Modo remoto/hosteado: HTTP con autenticación por token ---
        import sys
        from urllib.parse import parse_qs

        import uvicorn

        API_KEY = os.environ.get("MCP_API_KEY")
        if not API_KEY:
            print("ERROR: falta la variable MCP_API_KEY. El servidor no puede "
                  "arrancar en modo remoto sin un token.", file=sys.stderr)
            sys.exit(1)

        puerto = int(os.environ.get("PORT", "8000"))
        print(f"[finanzas-mcp] Arrancando en modo HTTP, puerto {puerto}", flush=True)
        print(f"[finanzas-mcp] Sheet objetivo: '{SHEET_NOMBRE}'", flush=True)
        print("[finanzas-mcp] La conexión a Google Sheets se hará al primer uso.",
              flush=True)

        def _extraer_token(scope) -> str:
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            tok = headers.get("x-api-key", "")
            if not tok:
                auth = headers.get("authorization", "")
                tok = auth[7:] if auth.lower().startswith("bearer ") else auth
            if not tok:
                qs = parse_qs(scope.get("query_string", b"").decode())
                tok = (qs.get("key") or qs.get("token") or [""])[0]
            return tok

        class AuthASGI:
            """Middleware ASGI puro: valida el token sin tocar el cuerpo de la
            respuesta, para no romper el streaming que usa MCP.
            Acepta el token por header x-api-key, Authorization: Bearer, o ?key= en la URL."""
            def __init__(self, app, api_key):
                self.app = app
                self.api_key = api_key

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)  # lifespan, etc.
                    return
                if _extraer_token(scope) != self.api_key:
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body",
                                "body": b'{"error":"no autorizado"}'})
                    return
                await self.app(scope, receive, send)

        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = puerto
        app = AuthASGI(mcp.streamable_http_app(), API_KEY)
        uvicorn.run(app, host="0.0.0.0", port=puerto)
    else:
        # --- Modo local: stdio para Claude Desktop ---
        mcp.run()
