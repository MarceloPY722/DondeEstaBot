"""
bot.py — Bot de Telegram para DondeEstaMiBus
Usa python-telegram-bot v20+ con ConversationHandler.

Variables de entorno requeridas:
    TELEGRAM_TOKEN  — Token del bot (de @BotFather)
"""

import os
import sys
import math
import time
import logging
import asyncio
import threading
import requests
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from dotenv import load_dotenv

# Cargar variables de entorno desde el archivo .env
load_dotenv()

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "TU_TOKEN_AQUI")

URL_API_JAHA = "https://www.jaha.com.py/api/posicionColectivos"
URL_OSRM = "http://router.project-osrm.org/route/v1/driving"

UMBRAL_ALERTA_MIN = 10      # avisar cuando el bus está a ≤ N minutos
INTERVALO_RASTREO_SEG = 60  # consultar cada 60 segundos (amigable con Jaha)
LINEAS_DISPONIBLES = ["187", "E1", "E2", "E3", "454"]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("BusBot")

# ============================================================
# ESTADOS DE LA CONVERSACION
# ============================================================
(
    ESPERANDO_LINEA,
    ESPERANDO_PARADA,
    ESPERANDO_DESTINO,
    ESPERANDO_SENTIDO,
    ESPERANDO_CONFIRMAR,
) = range(5)

# ============================================================
# UTILIDADES
# ============================================================

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def parsear_coords(texto: str):
    """
    Acepta formatos:
        -25.394565, -57.467389
        -25.394565370875533, -57.467389012047725
    Retorna (lat, lon) o None si falla.
    """
    texto = texto.strip().replace(" ", "")
    partes = texto.split(",")
    if len(partes) == 2:
        try:
            lat = float(partes[0])
            lon = float(partes[1])
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return lat, lon
        except ValueError:
            pass
    return None


def calcular_eta(lat_bus, lon_bus, lat_origen, lon_origen) -> float | None:
    """ETA en minutos desde el bus hasta la parada via OSRM."""
    try:
        url = (
            f"{URL_OSRM}/{lon_bus},{lat_bus};"
            f"{lon_origen},{lat_origen}?overview=false"
        )
        resp = requests.get(url, timeout=15)
        data = resp.json()
        segundos = data["routes"][0]["duration"]
        return round(segundos / 60, 1)
    except Exception as e:
        log.warning(f"OSRM error: {e}")
        return None


def obtener_buses(linea: str, lat_origen: float, lon_origen: float,
                  lat_destino: float, lon_destino: float, sentido: str) -> list:
    """
    Llama a la API de Jaha y filtra/procesa los buses.
    Retorna lista de dicts con info relevante.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
        resp = requests.post(
            URL_API_JAHA, headers=headers, json={"linea": linea}, timeout=20
        )
        if resp.status_code != 200:
            return []
        buses_raw = resp.json()
    except Exception as e:
        log.error(f"Error API Jaha: {e}")
        return []

    historial = {}  # simple dict local para este ciclo
    resultado = []

    for bus in buses_raw:
        unidad = bus.get("unidad")
        recorrido = bus.get("recorrido", "")
        aire = bus.get("aire", False)

        try:
            lat = float(bus.get("lat", 0))
            lon = float(bus.get("lon", 0))
        except (ValueError, TypeError):
            continue

        # Sentido
        rec_up = recorrido.upper()
        if "(I)" in rec_up or "IDA" in rec_up:
            sentido_bus = "IDA"
        elif "(V)" in rec_up or "VUELTA" in rec_up:
            sentido_bus = "VUELTA"
        else:
            sentido_bus = "DESCONOCIDO"

        if sentido_bus != sentido:
            continue

        # ¿Ya pasó la parada?
        dist_origen_destino = haversine(lat_origen, lon_origen, lat_destino, lon_destino)
        dist_bus_destino = haversine(lat, lon, lat_destino, lon_destino)
        if dist_bus_destino < (dist_origen_destino - 0.3):
            continue

        linea_real = recorrido.split("-")[0].strip() if "-" in recorrido else recorrido.strip()

        eta = calcular_eta(lat, lon, lat_origen, lon_origen)

        resultado.append({
            "unidad": unidad,
            "linea_real": linea_real,
            "sentido": sentido_bus,
            "aire": aire,
            "minutos": eta,
        })

    resultado.sort(key=lambda b: (b["minutos"] or 999))
    return resultado


# ============================================================
# TAREA DE RASTREO EN BACKGROUND
# ============================================================

class TareaRastreo:
    """Gestiona el loop de rastreo para un usuario."""

    def __init__(self, chat_id: int, config: dict, app: "Application"):
        self.chat_id = chat_id
        self.config = config
        self.app = app
        self.loop = asyncio.get_running_loop()
        self.activo = False
        self._hilo: threading.Thread | None = None
        self._alertas_enviadas: set = set()

    def iniciar(self):
        if self.activo:
            return
        self.activo = True
        self._hilo = threading.Thread(target=self._loop, daemon=True)
        self._hilo.start()

    def detener(self):
        self.activo = False

    def _loop(self):
        log.info(f"[Rastreo] Iniciado para chat {self.chat_id}")
        while self.activo:
            try:
                self._ciclo()
            except Exception as e:
                log.error(f"[Rastreo] Error en ciclo: {e}")
            # Esperar INTERVALO_RASTREO_SEG segundos, pero revisar .activo cada segundo
            for _ in range(INTERVALO_RASTREO_SEG):
                if not self.activo:
                    break
                time.sleep(1)
        log.info(f"[Rastreo] Detenido para chat {self.chat_id}")

    def _ciclo(self):
        cfg = self.config
        buses = obtener_buses(
            cfg["linea"],
            cfg["lat_origen"], cfg["lon_origen"],
            cfg["lat_destino"], cfg["lon_destino"],
            cfg["sentido"],
        )

        ahora_str = datetime.now().strftime("%H:%M")

        if not buses:
            msg = f"🚌 *{ahora_str}* — Sin buses de línea {cfg['linea']} en camino."
        else:
            lineas_msg = []
            for b in buses[:5]:  # máximo 5 buses
                eta_txt = f"{b['minutos']} min" if b["minutos"] is not None else "?"
                aire_txt = " ❄️" if b["aire"] else ""
                lineas_msg.append(f"  • Unidad #{b['unidad']} — *{eta_txt}*{aire_txt}")

            msg = f"🚌 *Línea {cfg['linea']} · {ahora_str}*\n" + "\n".join(lineas_msg)

        # Enviar actualización periódica
        asyncio.run_coroutine_threadsafe(
            self.app.bot.send_message(
                chat_id=self.chat_id,
                text=msg,
                parse_mode="Markdown",
            ),
            self.loop,
        )

        # Alertas especiales para buses cercanos
        for b in buses:
            if b["minutos"] is not None and b["minutos"] <= UMBRAL_ALERTA_MIN:
                clave = f"{b['unidad']}_{ahora_str}"
                if clave not in self._alertas_enviadas:
                    self._alertas_enviadas.add(clave)
                    aire_txt = " ❄️" if b["aire"] else ""
                    alerta = (
                        f"🔔 *¡ALERTA!* Unidad #{b['unidad']} de línea {cfg['linea']}"
                        f"{aire_txt} llega en *{b['minutos']} min*."
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.app.bot.send_message(
                            chat_id=self.chat_id,
                            text=alerta,
                            parse_mode="Markdown",
                        ),
                        self.loop,
                    )


# Almacena tareas activas por chat_id
_tareas: dict[int, TareaRastreo] = {}

# ============================================================
# HANDLERS DE CONVERSACION
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 *Bienvenido a DondeEstaMiBus*\n\n"
        "Usa /init para configurar tu ruta y empezar el rastreo.\n"
        "Usa /stop para detener el rastreo activo.",
        parse_mode="Markdown",
    )


async def cmd_init(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    # Detener rastreo previo si existe
    chat_id = update.effective_chat.id
    if chat_id in _tareas:
        _tareas[chat_id].detener()
        del _tareas[chat_id]

    ctx.user_data.clear()

    teclado = ReplyKeyboardMarkup(
        [[l] for l in LINEAS_DISPONIBLES],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "🚌 *Configuración de ruta*\n\n¿Qué línea querés rastrear?",
        reply_markup=teclado,
        parse_mode="Markdown",
    )
    return ESPERANDO_LINEA


async def recibir_linea(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    linea = update.message.text.strip().upper()
    if linea not in [l.upper() for l in LINEAS_DISPONIBLES]:
        await update.message.reply_text(
            f"❌ Línea no reconocida. Elegí una de: {', '.join(LINEAS_DISPONIBLES)}"
        )
        return ESPERANDO_LINEA

    ctx.user_data["linea"] = linea
    await update.message.reply_text(
        f"✅ Línea *{linea}* seleccionada.\n\n"
        "📍 Ahora enviame las coordenadas de *tu parada* en este formato:\n"
        "`-25.394565, -57.467389`",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return ESPERANDO_PARADA


async def recibir_parada(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    coords = parsear_coords(update.message.text)
    if not coords:
        await update.message.reply_text(
            "❌ No pude leer las coordenadas. Asegurate de usar el formato:\n"
            "`-25.394565, -57.467389`",
            parse_mode="Markdown",
        )
        return ESPERANDO_PARADA

    ctx.user_data["lat_origen"], ctx.user_data["lon_origen"] = coords
    await update.message.reply_text(
        f"✅ Parada guardada: `{coords[0]}, {coords[1]}`\n\n"
        "🏁 Ahora enviame las coordenadas de tu *destino*:",
        parse_mode="Markdown",
    )
    return ESPERANDO_DESTINO


async def recibir_destino(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    coords = parsear_coords(update.message.text)
    if not coords:
        await update.message.reply_text(
            "❌ No pude leer las coordenadas. Formato esperado:\n"
            "`-25.394565, -57.467389`",
            parse_mode="Markdown",
        )
        return ESPERANDO_DESTINO

    ctx.user_data["lat_destino"], ctx.user_data["lon_destino"] = coords
    teclado = ReplyKeyboardMarkup(
        [["IDA", "VUELTA"]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"✅ Destino guardado: `{coords[0]}, {coords[1]}`\n\n"
        "↔️ ¿Qué sentido necesitás?",
        reply_markup=teclado,
        parse_mode="Markdown",
    )
    return ESPERANDO_SENTIDO


async def recibir_sentido(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    sentido = update.message.text.strip().upper()
    if sentido not in ("IDA", "VUELTA"):
        await update.message.reply_text("❌ Respondé *IDA* o *VUELTA*.", parse_mode="Markdown")
        return ESPERANDO_SENTIDO

    ctx.user_data["sentido"] = sentido

    ud = ctx.user_data
    resumen = (
        f"📋 *Resumen de configuración*\n\n"
        f"• Línea: *{ud['linea']}*\n"
        f"• Sentido: *{ud['sentido']}*\n"
        f"• Parada: `{ud['lat_origen']}, {ud['lon_origen']}`\n"
        f"• Destino: `{ud['lat_destino']}, {ud['lon_destino']}`\n\n"
        f"¿Comenzar el rastreo?"
    )
    teclado = ReplyKeyboardMarkup(
        [["Sí", "No"]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )
    await update.message.reply_text(
        resumen,
        reply_markup=teclado,
        parse_mode="Markdown",
    )
    return ESPERANDO_CONFIRMAR


async def recibir_confirmacion(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    resp = update.message.text.strip().lower()
    if resp not in ("sí", "si", "s", "yes", "y"):
        await update.message.reply_text(
            "❌ Rastreo cancelado. Usá /init para empezar de nuevo.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    config = {
        "linea": ctx.user_data["linea"],
        "lat_origen": ctx.user_data["lat_origen"],
        "lon_origen": ctx.user_data["lon_origen"],
        "lat_destino": ctx.user_data["lat_destino"],
        "lon_destino": ctx.user_data["lon_destino"],
        "sentido": ctx.user_data["sentido"],
    }

    tarea = TareaRastreo(chat_id, config, ctx.application)
    _tareas[chat_id] = tarea
    tarea.iniciar()

    await update.message.reply_text(
        f"🟢 *Rastreo iniciado*\n\n"
        f"Te voy a avisar cada *{INTERVALO_RASTREO_SEG // 60} minuto(s)* sobre la línea "
        f"*{config['linea']}* ({config['sentido']}).\n\n"
        f"Recibirás una alerta especial 🔔 cuando el bus esté a ≤ {UMBRAL_ALERTA_MIN} minutos.\n\n"
        f"Usá /stop para detener.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id in _tareas:
        _tareas[chat_id].detener()
        del _tareas[chat_id]
        await update.message.reply_text(
            "🔴 Rastreo detenido. Usá /init para iniciar uno nuevo."
        )
    else:
        await update.message.reply_text("ℹ️ No hay rastreo activo.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if chat_id not in _tareas or not _tareas[chat_id].activo:
        await update.message.reply_text("ℹ️ No hay rastreo activo. Usá /init.")
        return

    cfg = _tareas[chat_id].config
    await update.message.reply_text(
        f"📡 *Rastreo activo*\n"
        f"• Línea: *{cfg['linea']}*\n"
        f"• Sentido: *{cfg['sentido']}*\n"
        f"• Parada: `{cfg['lat_origen']}, {cfg['lon_origen']}`\n"
        f"• Destino: `{cfg['lat_destino']}, {cfg['lon_destino']}`\n"
        f"• Intervalo: cada {INTERVALO_RASTREO_SEG}s",
        parse_mode="Markdown",
    )


async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "❌ Configuración cancelada. Usá /init para empezar de nuevo.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


# ============================================================
# MAIN
# ============================================================

def main():
    if TELEGRAM_TOKEN == "TU_TOKEN_AQUI":
        print("ERROR: Configurá la variable de entorno TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("init", cmd_init)],
        states={
            ESPERANDO_LINEA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_linea)],
            ESPERANDO_PARADA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_parada)],
            ESPERANDO_DESTINO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_destino)],
            ESPERANDO_SENTIDO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_sentido)],
            ESPERANDO_CONFIRMAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_confirmacion)],
        },
        fallbacks=[CommandHandler("cancel", cancelar)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(conv)

    print("🚌 Bot iniciado. Esperando mensajes...", flush=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
