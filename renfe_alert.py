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


def admin_id():
    return str(os.environ.get("TELEGRAM_CHAT_ID", ""))


def fecha_larga(f):
    return f"{DIAS_ES[f.weekday()].capitalize()} {f.day} {MESES[f.month - 1]}"


def clave_tren(fecha, hora, o, d):
    return f"{fecha.isoformat()}|{hora}|{o}-{d}"


def sentido(o):
    return "ida" if o == TRENES[0][1] else "vuelta"


def nombre_de(u):
    n = (u.get("first_name") or "") + (" " + u["last_name"] if u.get("last_name") else "")
    return (n.strip() or "Sin nombre") + (f" (@{u['username']})" if u.get("username") else "")


def html(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def texto_panel(estado, usuario, momento):
    fechas = fechas_objetivo()
    if not fechas:
        return None
    comprados = set(usuario.get("comprados", []))
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


def botones_panel(usuario, momento):
    comprados = set(usuario.get("comprados", []))
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


def actualizar_panel(estado, chat_id, momento=None, nuevo=False):
    """Mantiene un único mensaje fijado por persona con el estado de la semana y lo va editando."""
    usuario = estado["usuarios"].get(chat_id)
    if usuario is None:
        return
    momento = momento or dt.datetime.fromisoformat(estado.get("ultima", ahora().isoformat()))
    texto = texto_panel(estado, usuario, momento)
    if not texto:
        return
    teclado = botones_panel(usuario, momento)
    semana = fechas_objetivo()[0].isoformat()
    panel = usuario.get("panel") or {}
    if not nuevo and panel.get("id") and panel.get("semana") == semana:
        try:
            tg("editMessageText", chat_id=chat_id, message_id=panel["id"], text=texto,
               parse_mode="HTML", reply_markup=teclado)
            return
        except Exception:  # noqa: BLE001  (mensaje borrado, etc.) -> se crea otro
            pass
    msg = tg("sendMessage", chat_id=chat_id, text=texto, parse_mode="HTML",
             disable_notification="true", reply_markup=teclado)
    if msg:
        if panel.get("id"):
            try:
                tg("unpinChatMessage", chat_id=chat_id, message_id=panel["id"])
            except Exception:  # noqa: BLE001
                pass
        try:
            tg("pinChatMessage", chat_id=chat_id, message_id=msg["message_id"], disable_notification="true")
        except Exception:  # noqa: BLE001
            pass
        usuario["panel"] = {"id": msg["message_id"], "semana": semana}


def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            e = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        e = {}
    if "trenes" not in e:  # formato muy antiguo: claves sueltas
        e = {"trenes": {k: v for k, v in e.items() if "|" in k}}
    if "usuarios" not in e:  # formato de un solo usuario -> el admin
        e["usuarios"] = {admin_id(): {"nombre": "Admin",
                                      "comprados": e.pop("comprados", []),
                                      "panel": e.pop("panel", None)}}
    e["usuarios"].setdefault(admin_id(), {"nombre": "Admin", "comprados": []})
    e.setdefault("pendientes", {})
    e.setdefault("rechazados", [])
    return e


def guardar_estado(estado):
    hoy = ahora().date().isoformat()
    estado["trenes"] = {k: v for k, v in estado["trenes"].items() if k.split("|")[0] >= hoy}
    for u in estado["usuarios"].values():
        u["comprados"] = sorted(k for k in set(u.get("comprados", [])) if k.split("|")[0] >= hoy)
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1)


# ------------------------------------------------ mensajes y botones de Telegram ---
def responder(cb, texto=None):
    try:
        if texto:
            tg("answerCallbackQuery", callback_query_id=cb["id"], text=texto)
        else:
            tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:  # noqa: BLE001
        pass


def lista_usuarios(estado):
    filas = [[{"text": f"🗑️ Quitar a {u.get('nombre', cid)[:40]}", "callback_data": f"q|{cid}"}]
             for cid, u in estado["usuarios"].items() if cid != admin_id()]
    texto = "👥 <b>Personas con acceso</b>\n\n" + "\n".join(
        f"• {html(u.get('nombre', cid))}" for cid, u in estado["usuarios"].items() if cid != admin_id()
    ) if filas else "👥 De momento solo tienes acceso tú."
    tg("sendMessage", text=texto, parse_mode="HTML", reply_markup={"inline_keyboard": filas})


def procesar_mensaje(estado, m):
    chat = str(m.get("chat", {}).get("id"))
    if m.get("chat", {}).get("type") != "private":
        return
    texto = (m.get("text") or "").strip()
    if chat in estado["usuarios"]:
        if chat == admin_id() and texto.startswith("/usuarios"):
            lista_usuarios(estado)
        elif texto.startswith(("/start", "/panel")):
            actualizar_panel(estado, chat, nuevo=True)
        return
    if chat in estado["rechazados"]:
        return
    if chat in estado["pendientes"]:
        return  # ya pedido; no molestar otra vez
    nombre = nombre_de(m.get("from", {}))
    estado["pendientes"][chat] = {"nombre": nombre}
    tg("sendMessage", chat_id=chat, parse_mode="HTML",
       text="👋 ¡Hola! Este bot avisa cuando se liberan plazas en los Avant Segovia ⇄ Madrid.\n\n"
            "⏳ He pedido permiso para darte acceso. Te escribo en cuanto te acepten.")
    tg("sendMessage", parse_mode="HTML",
       text=f"👤 <b>{html(nombre)}</b> quiere usar el bot de avisos.",
       reply_markup={"inline_keyboard": [[{"text": "✅ Aceptar", "callback_data": f"a|{chat}"},
                                          {"text": "❌ Rechazar", "callback_data": f"r|{chat}"}]]})


def procesar_boton(estado, cb):
    quien = str(cb.get("from", {}).get("id"))
    datos = cb.get("data", "")
    tipo, _, arg = datos.partition("|")
    msg = cb.get("message") or {}

    if tipo in ("a", "r", "q"):
        if quien != admin_id():
            return responder(cb)
        persona = estado["pendientes"].pop(arg, None) or estado["usuarios"].get(arg) or {}
        nombre = persona.get("nombre", arg)
        if tipo == "a":
            estado["usuarios"][arg] = {"nombre": nombre, "comprados": []}
            if arg in estado["rechazados"]:
                estado["rechazados"].remove(arg)
            final = f"✅ <b>{html(nombre)}</b> ya tiene acceso."
            tg("sendMessage", chat_id=arg, parse_mode="HTML",
               text="✅ <b>¡Dentro!</b> Ya tienes acceso.\n\n"
                    "Te he fijado arriba un panel con los trenes de la semana. Cuando un tren lleno "
                    "tenga plazas te llegará un aviso 🟢, y puedes marcar con 🎫 los que ya tengas comprados.")
            actualizar_panel(estado, arg, nuevo=True)
            responder(cb, "Aceptado")
        elif tipo == "r":
            if arg not in estado["rechazados"]:
                estado["rechazados"].append(arg)
            final = f"❌ Has rechazado a <b>{html(nombre)}</b>."
            responder(cb, "Rechazado")
        else:  # quitar
            estado["usuarios"].pop(arg, None)
            if arg not in estado["rechazados"]:
                estado["rechazados"].append(arg)
            final = f"🗑️ <b>{html(nombre)}</b> ya no tiene acceso."
            try:
                tg("sendMessage", chat_id=arg, text="Tu acceso al bot de avisos de Renfe se ha retirado.")
            except Exception:  # noqa: BLE001
                pass
            responder(cb, "Quitado")
        if msg.get("message_id"):
            try:
                tg("editMessageText", message_id=msg["message_id"], text=final, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
        return

    if tipo == "c" and quien in estado["usuarios"]:
        usuario = estado["usuarios"][quien]
        comprados = set(usuario.get("comprados", []))
        fecha_s, hora, _ = arg.split("|")
        f = dt.date.fromisoformat(fecha_s)
        if arg in comprados:
            comprados.discard(arg)
            aviso = f"⬜ {DIAS_CORTOS[f.weekday()]} {f.day} {hora}: te vuelvo a avisar si se libera"
        else:
            comprados.add(arg)
            aviso = f"🎫 {DIAS_CORTOS[f.weekday()]} {f.day} {hora}: marcado como comprado"
        usuario["comprados"] = sorted(comprados)
        responder(cb, aviso)
        actualizar_panel(estado, quien)
        return
    responder(cb)


def atender_telegram(espera=25):
    """Espera hasta `espera` s a mensajes o pulsaciones de botones y los procesa."""
    if not os.environ.get("TELEGRAM_TOKEN"):
        time.sleep(espera)
        return
    estado = cargar_estado()
    try:
        updates = tg("getUpdates", offset=estado.get("offset", 0), timeout=espera,
                     allowed_updates='["message","callback_query"]') or []
    except Exception as e:  # noqa: BLE001
        print(f"getUpdates falló: {e}", file=sys.stderr)
        time.sleep(5)
        return
    for u in updates:
        estado["offset"] = u["update_id"] + 1
        try:
            if "callback_query" in u:
                procesar_boton(estado, u["callback_query"])
            elif "message" in u:
                procesar_mensaje(estado, u["message"])
        except Exception as e:  # noqa: BLE001
            print(f"Error procesando update: {e}", file=sys.stderr)
    if updates:
        guardar_estado(estado)


async def comprobar():
    from playwright.async_api import async_playwright

    estado = cargar_estado()
    momento = ahora()
    avisos = []
    # un trayecto solo se deja de mirar si TODOS lo tienen comprado
    todos_comprado = set.intersection(*[set(u.get("comprados", [])) for u in estado["usuarios"].values()])

    async with async_playwright() as p:
        navegador = await p.chromium.launch(headless=True)
        try:
            for fecha in fechas_objetivo():
                trayectos = {}
                for hora, o, d, nombre in TRENES:
                    salida = dt.datetime.combine(fecha, dt.time.fromisoformat(hora), TZ)
                    if salida > momento and clave_tren(fecha, hora, o, d) not in todos_comprado:
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
                    await asyncio.sleep(2)  # sin prisas con la web de Renfe
        finally:
            await navegador.close()

    # recargamos usuarios/compras por si cambiaron mientras se hacía la comprobación
    reciente = cargar_estado()
    for k in ("usuarios", "pendientes", "rechazados", "offset"):
        if k in reciente:
            estado[k] = reciente[k]
    for clave, fecha, hora, nombre, enlace in avisos:
        for chat_id, u in estado["usuarios"].items():
            if clave in u.get("comprados", []):
                continue
            try:
                tg("sendMessage", chat_id=chat_id, text=texto_aviso(fecha, hora, nombre), parse_mode="HTML",
                   reply_markup={"inline_keyboard": [[{"text": "🎟️ Reservar ahora", "url": enlace}]]})
            except Exception as e:  # noqa: BLE001
                print(f"No pude avisar a {chat_id}: {e}", file=sys.stderr)
    estado["ultima"] = momento.isoformat()
    for chat_id in list(estado["usuarios"]):
        try:
            actualizar_panel(estado, chat_id, momento)
        except Exception as e:  # noqa: BLE001
            print(f"No pude actualizar el panel de {chat_id}: {e}", file=sys.stderr)
    guardar_estado(estado)


def vigilar(minutos_total, cada=10):
    """Comprueba Renfe cada `cada` min y, entre medias, atiende al momento mensajes y botones."""
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
