import requests
import time
import threading
import math
from datetime import datetime
from flask import Flask, render_template, jsonify, request

# ============================================================
# CONFIGURACION DINAMICA
# ============================================================
CONFIG = {
    "linea": "187",
    "lat_origen": -25.393250,
    "lon_origen": -57.468139,
    "lat_destino": None,
    "lon_destino": None,
    "sentido_deseado": "IDA",
    "activo": False
}

URL_API_JAHA = "https://www.jaha.com.py/api/posicionColectivos"
URL_OSRM = "http://router.project-osrm.org/route/v1/driving"
UMBRAL_MINUTOS = 10
INTERVALO_SEGUNDOS = 30

# ============================================================
# ESTADO EN MEMORIA
# ============================================================
HISTORIAL_POSICIONES = {}
ALERTAS_ENVIADAS = set()
alertas_visibles = []
buses_monitor = []
last_update = None
errores = []

app = Flask(__name__)

# ============================================================
# LOGICA DE MONITOREO
# ============================================================
def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0 # Radio de la Tierra en km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def calcular_eta(lat_bus, lon_bus, lat_origen, lon_origen):
    """Calcula tiempo estimado en minutos via OSRM desde el bus hasta el Origen."""
    url = (
        f"{URL_OSRM}/{lon_bus},{lat_bus};"
        f"{lon_origen},{lat_origen}?overview=false"
    )
    resp = requests.get(url, timeout=15)
    data = resp.json()
    segundos = data["routes"][0]["duration"]
    return round(segundos / 60, 1)

def registrar_alerta(unidad, minutos, aire, recorrido_real):
    """Registra una alerta visible en la web."""
    ahora = datetime.now().strftime("%H:%M:%S")
    aire_txt = "SI" if aire else "NO"
    alerta = {
        "hora": ahora,
        "unidad": unidad,
        "minutos": minutos,
        "aire": aire_txt,
        "recorrido": recorrido_real,
        "mensaje": f"[{recorrido_real}] Unidad #{unidad} llega en {minutos} min (A/C: {aire_txt})",
    }
    alertas_visibles.append(alerta)
    if len(alertas_visibles) > 20:
        alertas_visibles.pop(0)
    log(f"[ALERTA] {alerta['mensaje']}")

def ciclo_monitoreo():
    """Un ciclo completo de monitoreo."""
    global buses_monitor, last_update, last_api_buses_call_time

    if not CONFIG["activo"]:
        return
        
    # Auto-stop if no frontend has polled in the last 15 seconds
    if time.time() - last_api_buses_call_time > 15:
        log("No hay clientes conectados. Pausando el escaneo en terminal.")
        CONFIG["activo"] = False
        return

    try:
        log(f"Consultando API Jaha para linea {CONFIG['linea']}...")
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/json",
        }
        payload = {"linea": CONFIG["linea"]}
        resp = requests.post(URL_API_JAHA, headers=headers, json=payload, timeout=20)
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:100]}")
        buses = resp.json()
        log(f"Recibidos {len(buses)} buses de la API")
    except Exception as e:
        msg = f"Error API Jaha: {e}"
        log(msg)
        errores.append(f"{datetime.now().strftime('%H:%M:%S')} - {msg}")
        return

    buses_filtrados = []
    ahora = datetime.now().strftime("%H:%M:%S")

    for bus in buses:
        unidad = bus.get("unidad")
        lat_str = bus.get("lat", "0")
        lon_str = bus.get("lon", "0")
        recorrido = bus.get("recorrido", "")
        estado = bus.get("estado", "")
        aire = bus.get("aire", False)

        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except (ValueError, TypeError):
            continue

        # Historial de posiciones
        if unidad not in HISTORIAL_POSICIONES:
            HISTORIAL_POSICIONES[unidad] = []
        HISTORIAL_POSICIONES[unidad].append({"lat": lat, "lon": lon})
        if len(HISTORIAL_POSICIONES[unidad]) > 3:
            HISTORIAL_POSICIONES[unidad].pop(0)

        # Extraer linea real (ej. de "E3-IDA (I)" sacar "E3")
        linea_real = recorrido.split('-')[0].strip() if '-' in recorrido else recorrido.strip()

        # Memoria de estado anterior
        estado_anterior = None
        if len(HISTORIAL_POSICIONES[unidad]) >= 2:
            estado_anterior = HISTORIAL_POSICIONES[unidad][-2].get("es_correcto")

        # Determinar Sentido
        recorrido_str = recorrido.upper()
        es_ida_str = "(I)" in recorrido_str or "IDA" in recorrido_str
        es_vuelta_str = "(V)" in recorrido_str or "VUELTA" in recorrido_str
        
        sentido_inferido = "DESCONOCIDO"
        if es_ida_str:
            sentido_inferido = "IDA"
        elif es_vuelta_str:
            sentido_inferido = "VUELTA"

        # Validacion por coordenada
        es_sentido_correcto = (sentido_inferido == CONFIG["sentido_deseado"])
        metodo = "STRING"
        
        if CONFIG["lat_origen"] is not None and CONFIG["lon_origen"] is not None:
            historial = HISTORIAL_POSICIONES.get(unidad, [])
            if len(historial) >= 2:
                # Calculamos si se acerca al origen
                dist_origen_ant = haversine(historial[-2]["lat"], historial[-2]["lon"], CONFIG["lat_origen"], CONFIG["lon_origen"])
                dist_origen_act = haversine(lat, lon, CONFIG["lat_origen"], CONFIG["lon_origen"])
                
                cambio = dist_origen_ant - dist_origen_act
                
                if cambio > 0.02: # Se acercó más de 20m al origen
                    es_sentido_correcto = True
                    metodo = "COORD_ACERCANDOSE"
                elif cambio < -0.05: # Se alejó más de 50m
                    es_sentido_correcto = False
                    metodo = "COORD_ALEJANDOSE"
                else:
                    # Si el bus está detenido o se mueve muy poco, confiamos en la memoria de lo que venía haciendo
                    if estado_anterior is not None:
                        es_sentido_correcto = estado_anterior
                        metodo = "MEMORIA_DETENIDO"
                    else:
                        # Si es la primera vez que lo vemos y está detenido, usamos la regla del usuario:
                        # Si está al sur del origen (lat menor) y vamos al norte (IPS), asumimos que es IDA
                        if lat < CONFIG["lat_origen"]:
                            es_sentido_correcto = True
                            metodo = "HEURISTICA_SUR"

        # Guardamos el resultado en el historial para la próxima iteración
        HISTORIAL_POSICIONES[unidad][-1]["es_correcto"] = es_sentido_correcto

        if not es_sentido_correcto:
            continue

        # Filtro: ¿Ya pasó la parada?
        # Heurística: Si el bus está más cerca del destino que la propia parada,
        # y va hacia el destino, entonces ya te pasó.
        ya_paso = False
        if CONFIG["lat_origen"] is not None and CONFIG["lon_origen"] is not None and CONFIG["lat_destino"] is not None and CONFIG["lon_destino"] is not None:
            dist_origen_destino = haversine(CONFIG["lat_origen"], CONFIG["lon_origen"], CONFIG["lat_destino"], CONFIG["lon_destino"])
            dist_bus_destino = haversine(lat, lon, CONFIG["lat_destino"], CONFIG["lon_destino"])
            
            # Margen de 300 metros por la geometría de las calles
            if dist_bus_destino < (dist_origen_destino - 0.3):
                ya_paso = True
                log(f"  Unidad {unidad} descartada: Ya pasó la parada.")

        if ya_paso:
            continue

        # Calcular ETA hacia el ORIGEN
        minutos = None
        if CONFIG["lat_origen"] is not None and CONFIG["lon_origen"] is not None:
            # Calcular distancia en linea recta al origen para un filtro rapido de lejanía (si ya pasó, se alejará)
            dist_al_origen = haversine(lat, lon, CONFIG["lat_origen"], CONFIG["lon_origen"])
            
            try:
                minutos = calcular_eta(lat, lon, CONFIG["lat_origen"], CONFIG["lon_origen"])
            except Exception as e:
                log(f"Error OSRM unidad {unidad}: {e}")

        info = {
            "unidad": unidad,
            "lat": lat,
            "lon": lon,
            "recorrido": recorrido,
            "linea_real": linea_real,
            "estado": estado,
            "aire": aire,
            "sentido": sentido_inferido,
            "minutos": minutos,
            "ultima_actualizacion": ahora,
            "metodo_validacion": metodo
        }
        buses_filtrados.append(info)

        # Alertas
        clave_alerta = f"{unidad}_{ahora[:5]}"
        if minutos is not None and minutos <= UMBRAL_MINUTOS:
            if clave_alerta not in ALERTAS_ENVIADAS:
                registrar_alerta(unidad, minutos, aire, linea_real)
                ALERTAS_ENVIADAS.add(clave_alerta)

    buses_monitor = buses_filtrados
    last_update = ahora

    log(f"Buses linea {CONFIG['linea']} en camino: {len(buses_filtrados)}")
    for b in buses_filtrados:
        aire_icon = "[A/C]" if b["aire"] else ""
        eta = f"{b['minutos']} min" if b["minutos"] is not None else "?"
        log(f"  [{b['linea_real']}] Unidad {b['unidad']} | {eta} | {aire_icon} | ValidadoPor: {b['metodo_validacion']}")


def bucle_monitoreo():
    log("=== MONITOR COLECTIVO INICIADO ===")
    while True:
        try:
            if CONFIG["activo"]:
                ciclo_monitoreo()
        except Exception as e:
            log(f"Error en ciclo: {e}")
            errores.append(f"{datetime.now().strftime('%H:%M:%S')} - {e}")
        time.sleep(INTERVALO_SEGUNDOS)


# ============================================================
# RUTAS WEB
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.json
    CONFIG["linea"] = data.get("linea", CONFIG["linea"])
    CONFIG["lat_origen"] = data.get("lat_origen", CONFIG["lat_origen"])
    CONFIG["lon_origen"] = data.get("lon_origen", CONFIG["lon_origen"])
    CONFIG["lat_destino"] = data.get("lat_destino", CONFIG["lat_destino"])
    CONFIG["lon_destino"] = data.get("lon_destino", CONFIG["lon_destino"])
    CONFIG["sentido_deseado"] = data.get("sentido_deseado", CONFIG["sentido_deseado"])
    CONFIG["activo"] = True
    
    # Limpiar alertas y buses al cambiar la config
    global alertas_visibles, buses_monitor, ALERTAS_ENVIADAS, last_api_buses_call_time
    alertas_visibles = []
    buses_monitor = []
    ALERTAS_ENVIADAS = set()
    last_api_buses_call_time = time.time()
    
    log(f"Nueva configuración recibida: {CONFIG}")
    
    # Forzar un ciclo de monitoreo inmediatamente en background
    threading.Thread(target=ciclo_monitoreo).start()
    
    return jsonify({"status": "ok", "config": CONFIG})

last_api_buses_call_time = 0

@app.route("/api/buses")
def api_buses():
    global last_api_buses_call_time
    last_api_buses_call_time = time.time()
    return jsonify({
        "buses": buses_monitor,
        "alertas": alertas_visibles,
        "last_update": last_update,
        "config": CONFIG,
        "errores": errores[-10:],
    })


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    hilo = threading.Thread(target=bucle_monitoreo, daemon=True)
    hilo.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
