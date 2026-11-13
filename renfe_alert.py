#!/usr/bin/env python3
"""
Avisos de plazas libres en Renfe -> Telegram.

Vigila los trenes configurados abajo (por defecto el Avant Segovia-Guiomar -> Chamartín
de las 08:01 y el Chamartín -> Segovia-Guiomar de las 20:02) de lunes a jueves de los
próximos 7 días, y te manda un mensaje de Telegram cuando un tren que estaba lleno
vuelve a tener plazas.

Uso:
  python renfe_alert.py                 # una comprobación (para GitHub Actions o cron)
  python renfe_alert.py --bucle 10      # comprueba cada 10 minutos sin parar (en tu PC)
  python renfe_alert.py --test          # manda un mensaje de prueba a Telegram
  python renfe_alert.py --chat-id       # te dice tu chat_id (escríbele antes algo al bot)

Variables de entorno:
  TELEGRAM_TOKEN    token del bot (te lo da @BotFather)
  TELEGRAM_CHAT_ID  tu chat_id
  ESTADO            ruta del fichero de estado (por defecto estado.json)
"""

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# CONFIGURACIÓN: cambia aquí trenes, días o códigos de estación
# ----------------------------------------------------------------------------
SEGOVIA_GUIOMAR = "08004"
MADRID_CHAMARTIN = "17000"

TRENES = [
    # (hora de salida, origen, destino, nombre para el mensaje)
    ("08:01", SEGOVIA_GUIOMAR, MADRID_CHAMARTIN, "Segovia → Madrid"),
    ("20:02", MADRID_CHAMARTIN, SEGOVIA_GUIOMAR, "Madrid → Segovia"),
]

DIAS_SEMANA = {0, 1, 2, 3}   # 0 = lunes ... 3 = jueves
DIAS_ADELANTE = 7            # cuántos días hacia delante mira
AVISAR_SI_SE_LLENA = False   # True = también avisa cuando un tren libre se llena
# ----------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Madrid")
ESTADO = os.environ.get("ESTADO", "estado.json")
DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# JS que busca elementos atravesando shadow DOM (la web de Renfe usa web components)
JS_LANZAR_BUSQUEDA = r"""
async () => {
  const all = (root, acc = []) => {
    root.querySelectorAll('*').forEach(e => { acc.push(e); if (e.shadowRoot) all(e.shadowRoot, acc); });
    return acc;
  };
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // Rechazar cookies no esenciales si sale el banner
  for (let i = 0; i < 10; i++) {
    const b = all(document).find(e => e.tagName === 'BUTTON' && /solo cookies t[eé]cnicas/i.test(e.textContent));
    if (b && b.offsetParent) { b.click(); break; }
    await sleep(300);
  }
  // Marcar "Solo ida"
  let radio = null;
  for (let i = 0; i < 50 && !radio; i++) { radio = all(document).find(e => e.id === 'radio-departure'); if (!radio) await sleep(300); }
  if (!radio) return 'no-radio';
  radio.click();
  // Pulsar "Buscar billetes" cuando se habilite
  for (let i = 0; i < 30; i++) {
    const btn = all(document).find(e => e.tagName === 'BUTTON' && e.textContent.trim() === 'Buscar billetes');
    if (btn && !btn.disabled) { setTimeout(() => btn.click(), 50); return 'ok'; }
    await sleep(300);
  }
  return 'no-boton';
}
"""


def ahora():
    return dt.datetime.now(TZ)


def fechas_objetivo():
    hoy = ahora().date()
    return [hoy + dt.timedelta(days=i) for i in range(DIAS_ADELANTE + 1)
            if (hoy + dt.timedelta(days=i)).weekday() in DIAS_SEMANA]


def url_busqueda(origen, destino, fecha):
    return (f"https://www.renfe.com/es/es/buscador-billetes?origin={origen}"
            f"&destination={destino}&departure-date={fecha.isoformat()}")


def extraer_objeto_dwr(texto):
    """La respuesta DWR es JS: r.handleCallback("x","y",{...});  Devolvemos el {...}."""
    i = texto.find("handleCallback(")
    if i < 0:
        raise ValueError("respuesta sin handleCallback")
    j = texto.find("{", i)
    k = texto.rfind("});")
    if j < 0 or k < j:
        raise ValueError("no encuentro el objeto en la respuesta")
    return texto[j:k + 1]


def estado_tren(viajes, hora):
    """'libre', 'lleno' o 'no_encontrado' para el tren que sale a `hora`."""
    candidatos = [v for v in viajes if v.get("horaSalida") == hora]
    if not candidatos:
        return "no_encontrado"
    for v in candidatos:
        if (not v.get("completo") and not v.get("soloPlazaH")
                and v.get("tarifasDisponibles")):
            return "libre"
    return "lleno"


async def buscar(navegador, origen, destino, fecha, intentos=2):
    ultimo_error = None
    for _ in range(intentos):
        ctx = await navegador.new_context(
            locale="es-ES",
            timezone_id="Europe/Madrid",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        try:
            await page.goto(url_busqueda(origen, destino, fecha), wait_until="domcontentloaded", timeout=60000)
            async with page.expect_response(lambda r: "getTrainsList" in r.url, timeout=60000) as info:
                res = await page.evaluate(JS_LANZAR_BUSQUEDA)
                if res != "ok":
                    raise RuntimeError(f"no se pudo lanzar la búsqueda ({res})")
            texto = await (await info.value).text()
            await page.wait_for_load_state("domcontentloaded")
            obj = extraer_objeto_dwr(texto)
            datos = json.loads(await page.evaluate(
                "s => JSON.stringify((new Function('return ' + s))())", obj))
            viajes = []
            for bloque in datos.get("listadoTrenes") or []:
                viajes.extend(bloque.get("listviajeViewEnlaceBean") or [])
            return viajes
        except Exception as e:  # noqa: BLE001
            ultimo_error = e
        finally:
            await ctx.close()
    raise RuntimeError(f"fallo buscando {origen}->{destino} {fecha}: {ultimo_error}")


def tg(metodo, **params):
    """Llama a la API de Telegram. Devuelve el 'result' o None si no hay Telegram configurado."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print(f"[sin Telegram] {metodo}: {params.get('text', '')}")
        return None
    params.setdefault("chat_id", chat)
    if "reply_markup" in params:
        params["reply_markup"] = json.dumps(params["reply_markup"])
    datos = urllib.parse.urlencode(params).encode()
    try:
        espera = int(params.get("timeout", 0)) + 20
        r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{metodo}", datos, timeout=espera))
        return r.get("result")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode(errors="ignore")
        if "message is not modified" not in cuerpo:
            print(f"Telegram {metodo} falló: {cuerpo}", file=sys.stderr)
            raise
        return None


def telegram(texto, botones=None, silencioso=False):
    params = {"text": texto, "parse_mode": "HTML", "disable_web_page_preview": "true",
              "disable_notification": "true" if silencioso else "false"}
    if botones:
        params["reply_markup"] = {"inline_keyboard": botones}
    return tg("sendMessage", **params)


# ---------------------------------------------------------------- mensajes ---
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS_CORTOS = ["LUN", "MAR", "MIÉ", "JUE", "VIE", "SÁB", "DOM"]
ICONO = {"libre": "🟢", "lleno": "🔴", "no_encontrado": "⚪", None: "⏳"}
COMPRADO = "🎫"


def fecha_larga(f):
    return f"{DIAS_ES[f.weekday()].capitalize()} {f.day} {MESES[f.month - 1]}"


def clave_tren(fecha, hora, o, d):
    return f"{fecha.isoformat()}|{hora}|{o}-{d}"


def sentido(o):
    return "ida" if o == TRENES[0][1] else "vuelta"


def texto_panel(estado, momento):
    fechas = fechas_objetivo()
    if not fechas:
        return None
    comprados = set(estado.get("comprados", []))
    rango = f"{fechas[0].day}–{fechas[-1].day} {MESES[fechas[-1].month - 1]}"
    lineas = [f"🚆 <b>TUS TRENES</b>  ·  <i>{rango}</i>", ""]
    for f in fechas:
        celdas = []
        for hora, o, d, _ in TRENES:
            clave = clave_tren(f, hora, o, d)
            salida = dt.datetime.combine(f, dt.time.fromisoformat(hora), TZ)
            if salida <= momento:
                icono = "✔️"
            elif clave in comprados:
                icono = COMPRADO
            else:
                icono = ICONO.get(estado["trenes"].get(clave), "⏳")
            celdas.append(f"{icono} <code>{hora}</code> {sentido(o)}")
        lineas.append(f"<b>{DIAS_CORTOS[f.weekday()]} {f.day:02d}</b>   " + "    ".join(celdas))
    lineas += ["", f"🟢 hay plazas   🔴 lleno   {COMPRADO} ya comprado   ✔️ ya salió",
               f"<i>🔄 Actualizado a las {momento:%H:%M} · se refresca cada 10 min</i>",
               "<i>👇 Toca un trayecto para marcarlo como comprado (y otra vez para desmarcarlo)</i>"]
    return "\n".join(lineas)


def botones_panel(estado, momento):
    comprados = set(estado.get("comprados", []))
    filas = []
    for f in fechas_objetivo():
        fila = []
        for hora, o, d, _ in TRENES:
            if dt.datetime.combine(f, dt.time.fromisoformat(hora), TZ) <= momento:
                continue
            clave = clave_tren(f, hora, o, d)
            marca = COMPRADO if clave in comprados else "⬜"
            fila.append({"text": f"{marca} {DIAS_CORTOS[f.weekday()]} {f.day} · {sentido(o)}",
                         "callback_data": "c|" + clave})
        if fila:
            filas.append(fila)
    return {"inline_keyboard": filas}


def texto_aviso(fecha, hora, nombre):
    return ("🟢 <b>¡PLAZA LIBRE!</b>\n\n"
            f"<blockquote><b>{fecha_larga(fecha)}</b>\n"
            f"🚄 <code>{hora}</code>  {nombre}</blockquote>\n"
            "<i>Corre, que estas plazas vuelan</i> ⚡")


def actualizar_panel(estado, momento=None):
    """Mantiene un único mensaje fijado con el estado de la semana y lo va editando."""
    momento = momento or dt.datetime.fromisoformat(estado.get("ultima", ahora().isoformat()))
    texto = texto_panel(estado, momento)
    if not texto:
        return
    teclado = botones_panel(estado, momento)
    semana = fechas_objetivo()[0].isoformat()
    panel = estado.get("panel") or {}
    if panel.get("id") and panel.get("semana") == semana:
        try:
            tg("editMessageText", message_id=panel["id"], text=texto, parse_mode="HTML",
               reply_markup=teclado)
            return
        except Exception:  # noqa: BLE001  (mensaje borrado, etc.) -> se crea otro
            pass
    msg = tg("sendMessage", text=texto, parse_mode="HTML", disable_notification="true",
             reply_markup=teclado)
    if msg:
        if panel.get("id"):
            try:
                tg("unpinChatMessage", message_id=panel["id"])
            except Exception:  # noqa: BLE001
                pass
        try:
            tg("pinChatMessage", message_id=msg["message_id"], disable_notification="true")
        except Exception:  # noqa: BLE001
            pass
        estado["panel"] = {"id": msg["message_id"], "semana": semana}


def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            e = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        e = {}
    if "trenes" not in e:  # formato antiguo: claves sueltas
        e = {"trenes": {k: v for k, v in e.items() if "|" in k}}
    e.setdefault("comprados", [])
    return e


def guardar_estado(estado):
    hoy = ahora().date().isoformat()
    estado["trenes"] = {k: v for k, v in estado["trenes"].items() if k.split("|")[0] >= hoy}
    estado["comprados"] = sorted(k for k in set(estado.get("comprados", [])) if k.split("|")[0] >= hoy)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------- botones del panel ---
def atender_telegram(espera=25):
    """Espera hasta `espera` s a que pulses un botón del panel y lo procesa. Devuelve True si hubo cambios."""
    if not os.environ.get("TELEGRAM_TOKEN"):
        time.sleep(espera)
        return False
    estado = cargar_estado()
    try:
        updates = tg("getUpdates", offset=estado.get("offset", 0), timeout=espera,
                     allowed_updates='["callback_query"]') or []
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates falló: {e}", file=sys.stderr)
        time.sleep(5)
        return False
    cambios = False
    mi_chat = str(os.environ.get("TELEGRAM_CHAT_ID"))
    for u in updates:
        estado["offset"] = u["update_id"] + 1
        cb = u.get("callback_query")
        if not cb:
            continue
        datos = cb.get("data", "")
        if str(cb.get("from", {}).get("id")) != mi_chat or not datos.startswith("c|"):
            tg("answerCallbackQuery", callback_query_id=cb["id"])
            continue
        clave = datos[2:]
        comprados = set(estado.get("comprados", []))
        fecha_s, hora, _ = clave.split("|")
        f = dt.date.fromisoformat(fecha_s)
        if clave in comprados:
            comprados.discard(clave)
            aviso = f"⬜ {DIAS_CORTOS[f.weekday()]} {f.day} {hora}: te vuelvo a avisar si se libera"
        else:
            comprados.add(clave)
            aviso = f"🎫 {DIAS_CORTOS[f.weekday()]} {f.day} {hora}: marcado como comprado"
        estado["comprados"] = sorted(comprados)
        try:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text=aviso)
        except Exception:  # noqa: BLE001
            pass
        cambios = True
    if updates:
        if cambios:
            actualizar_panel(estado)
        guardar_estado(estado)
    return cambios


async def comprobar():
    from playwright.async_api import async_playwright

    estado = cargar_estado()
    momento = ahora()
    avisos = []

    async with async_playwright() as p:
        navegador = await p.chromium.launch(headless=True)
        try:
            for fecha in fechas_objetivo():
                # agrupamos trenes por trayecto para hacer una sola búsqueda por día y sentido
                trayectos = {}
                for hora, o, d, nombre in TRENES:
                    salida = dt.datetime.combine(fecha, dt.time.fromisoformat(hora), TZ)
                    if salida > momento and clave_tren(fecha, hora, o, d) not in estado["comprados"]:
                        trayectos.setdefault((o, d), []).append((hora, nombre))
                for (o, d), trenes in trayectos.items():
                    try:
                        viajes = await buscar(navegador, o, d, fecha)
                    except Exception as e:  # noqa: BLE001
                        print(f"ERROR: {e}", file=sys.stderr)
                        continue
                    for hora, nombre in trenes:
                        clave = clave_tren(fecha, hora, o, d)
                        nuevo = estado_tren(viajes, hora)
                        viejo = estado["trenes"].get(clave)
                        estado["trenes"][clave] = nuevo
                        print(f"{ICONO.get(nuevo)} {fecha} {hora} {nombre}: {nuevo} (antes: {viejo})")
                        if nuevo == "libre" and viejo in ("lleno", "no_encontrado"):
                            avisos.append((clave, fecha, hora, nombre, url_busqueda(o, d, fecha)))
                        elif AVISAR_SI_SE_LLENA and nuevo == "lleno" and viejo == "libre":
                            telegram(f"🔴 Se ha llenado el <code>{hora}</code> del {fecha_larga(fecha).lower()}",
                                     silencioso=True)
                    await asyncio.sleep(2)  # sin prisas con la web de Renfe
        finally:
            await navegador.close()

    # por si marcaste algo como comprado mientras se hacía la comprobación
    estado["comprados"] = cargar_estado()["comprados"]
    for clave, fecha, hora, nombre, enlace in avisos:
        if clave in estado["comprados"]:
            continue
        telegram(texto_aviso(fecha, hora, nombre),
                 botones=[[{"text": "🎟️ Reservar ahora", "url": enlace}]])
    estado["ultima"] = momento.isoformat()
    actualizar_panel(estado, momento)
    guardar_estado(estado)


def vigilar(minutos_total, cada=10):
    """Comprueba Renfe cada `cada` min y, entre medias, atiende al momento los botones del panel."""
    fin = time.time() + minutos_total * 60
    proxima = 0.0
    while time.time() < fin:
        if time.time() >= proxima:
            if ahora().hour >= 6:  # de 0:00 a 6:00 no mira Renfe
                try:
                    asyncio.run(comprobar())
                except Exception as e:  # noqa: BLE001
                    print(f"ERROR en la comprobación: {e}", file=sys.stderr)
            proxima = time.time() + cada * 60
        atender_telegram(espera=int(min(25, max(1, proxima - time.time()))))


def mostrar_chat_id():
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        sys.exit("Define TELEGRAM_TOKEN primero.")
    r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20))
    ids = {m["message"]["chat"]["id"] for m in r.get("result", []) if "message" in m}
    print(ids or "No hay mensajes: escríbele cualquier cosa a tu bot y vuelve a ejecutar.")


def main():
    ap = argparse.ArgumentParser(description="Avisos de plazas libres en Renfe por Telegram")
    ap.add_argument("--bucle", type=int, metavar="MIN", help="repetir cada MIN minutos")
    ap.add_argument("--test", action="store_true", help="mandar un mensaje de prueba")
    ap.add_argument("--chat-id", action="store_true", help="mostrar tu chat_id")
    ap.add_argument("--vigilar", type=int, metavar="MIN",
                    help="vigilar durante MIN minutos (Renfe cada 10 min + botones al instante)")
    a = ap.parse_args()

    if a.chat_id:
        return mostrar_chat_id()
    if a.test:
        return telegram("✅ El bot de avisos de Renfe funciona.")
    if a.vigilar:
        return vigilar(a.vigilar)
    if a.bucle:
        return vigilar(10**9, cada=a.bucle)
    else:
        asyncio.run(comprobar())


if __name__ == "__main__":
    main()
