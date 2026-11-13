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
        r = json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{metodo}", datos, timeout=20))
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


def fecha_larga(f):
    return f"{DIAS_ES[f.weekday()].capitalize()} {f.day} {MESES[f.month - 1]}"


def texto_panel(trenes_estado, momento):
    fechas = fechas_objetivo()
    if not fechas:
        return None
    rango = f"{fechas[0].day}–{fechas[-1].day} {MESES[fechas[-1].month - 1]}"
    lineas = [f"🚆 <b>TUS TRENES</b>  ·  <i>{rango}</i>", ""]
    for f in fechas:
        celdas = []
        for hora, o, d, nombre in TRENES:
            salida = dt.datetime.combine(f, dt.time.fromisoformat(hora), TZ)
            if salida <= momento:
                icono = "✔️"
            else:
                icono = ICONO.get(trenes_estado.get(f"{f.isoformat()}|{hora}|{o}-{d}"), "⏳")
            sentido = "ida" if o == TRENES[0][1] else "vuelta"
            celdas.append(f"{icono} <code>{hora}</code> {sentido}")
        lineas.append(f"<b>{DIAS_CORTOS[f.weekday()]} {f.day:02d}</b>   " + "    ".join(celdas))
    lineas += ["", "🟢 hay plazas   🔴 lleno   ✔️ ya salió",
               f"<i>🔄 Actualizado a las {momento:%H:%M} · se refresca cada 10 min</i>"]
    return "\n".join(lineas)


def texto_aviso(fecha, hora, nombre):
    return ("🟢 <b>¡PLAZA LIBRE!</b>\n\n"
            f"<blockquote><b>{fecha_larga(fecha)}</b>\n"
            f"🚄 <code>{hora}</code>  {nombre}</blockquote>\n"
            "<i>Corre, que estas plazas vuelan</i> ⚡")


def actualizar_panel(estado, momento):
    """Mantiene un único mensaje fijado con el estado de la semana y lo va editando."""
    texto = texto_panel(estado["trenes"], momento)
    if not texto:
        return
    semana = fechas_objetivo()[0].isoformat()
    panel = estado.get("panel") or {}
    if panel.get("id") and panel.get("semana") == semana:
        try:
            tg("editMessageText", message_id=panel["id"], text=texto, parse_mode="HTML")
            return
        except Exception:  # noqa: BLE001  (mensaje borrado, etc.) -> se crea otro
            pass
    msg = telegram(texto, silencioso=True)
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
    return e


def guardar_estado(estado):
    hoy = ahora().date().isoformat()
    estado["trenes"] = {k: v for k, v in estado["trenes"].items() if k.split("|")[0] >= hoy}
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


async def comprobar():
    from playwright.async_api import async_playwright

    estado = cargar_estado()
    trenes_estado = estado["trenes"]
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
                    if salida > momento:
                        trayectos.setdefault((o, d), []).append((hora, nombre))
                for (o, d), trenes in trayectos.items():
                    try:
                        viajes = await buscar(navegador, o, d, fecha)
                    except Exception as e:  # noqa: BLE001
                        print(f"ERROR: {e}", file=sys.stderr)
                        continue
                    for hora, nombre in trenes:
                        clave = f"{fecha.isoformat()}|{hora}|{o}-{d}"
                        nuevo = estado_tren(viajes, hora)
                        viejo = trenes_estado.get(clave)
                        trenes_estado[clave] = nuevo
                        print(f"{ICONO.get(nuevo)} {fecha} {hora} {nombre}: {nuevo} (antes: {viejo})")
                        # avisa si pasa de lleno a libre (no en la primera lectura de ese tren)
                        if nuevo == "libre" and viejo in ("lleno", "no_encontrado"):
                            avisos.append((fecha, hora, nombre, url_busqueda(o, d, fecha)))
                        elif AVISAR_SI_SE_LLENA and nuevo == "lleno" and viejo == "libre":
                            telegram(f"🔴 Se ha llenado el <code>{hora}</code> del {fecha_larga(fecha).lower()}",
                                     silencioso=True)
                    await asyncio.sleep(2)  # sin prisas con la web de Renfe
        finally:
            await navegador.close()

    for fecha, hora, nombre, enlace in avisos:
        telegram(texto_aviso(fecha, hora, nombre),
                 botones=[[{"text": "🎟️ Reservar ahora", "url": enlace}]])
    actualizar_panel(estado, momento)
    guardar_estado(estado)


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
    a = ap.parse_args()

    if a.chat_id:
        return mostrar_chat_id()
    if a.test:
        return telegram("✅ El bot de avisos de Renfe funciona.")
    if a.bucle:
        while True:
            h = ahora().hour
            if 5 <= h < 23:  # de noche no mira
                try:
                    asyncio.run(comprobar())
                except Exception as e:  # noqa: BLE001
                    print(f"ERROR: {e}", file=sys.stderr)
            time.sleep(a.bucle * 60)
    else:
        asyncio.run(comprobar())


if __name__ == "__main__":
    main()
