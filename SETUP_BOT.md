# 🚌 DondeEstaMiBus — Bot de Telegram

## Cómo configurar el bot

### 1. Crear el bot en Telegram

1. Abrí Telegram y buscá **@BotFather**
2. Mandá `/newbot`
3. Seguí las instrucciones (nombre, username)
4. BotFather te da un **token** con este formato:
   ```
   123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
5. Guardalo, lo vas a necesitar.

### 2. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar el token

**Opción A — Variable de entorno (recomendado):**
```bash
export TELEGRAM_TOKEN="123456789:AAFxxxxxxxxxx"
python bot.py
```

**Opción B — Editar directamente en bot.py:**
```python
TELEGRAM_TOKEN = "123456789:AAFxxxxxxxxxx"
```

### 4. Correr el bot

```bash
python bot.py
```

El bot y la app web son **independientes**. Podés correr solo el bot sin Flask, o ambos al mismo tiempo.

---

## Comandos disponibles

| Comando | Descripción |
|---------|-------------|
| `/start` | Mensaje de bienvenida |
| `/init` | Iniciar configuración de ruta (flujo completo) |
| `/stop` | Detener el rastreo activo |
| `/status` | Ver la configuración actual del rastreo |
| `/cancel` | Cancelar la configuración en curso |

---

## Flujo de conversación

```
/init
  └─> Elegí línea: 187 / E1 / E2 / E3 / 454
        └─> Enviá coordenadas de tu parada: -25.394565, -57.467389
              └─> Enviá coordenadas de tu destino: -25.307677, -57.593271
                    └─> Elegí sentido: IDA / VUELTA
                          └─> ¿Comenzar rastreo? Sí / No
                                └─> 🟢 Rastreo activo
```

---

## Comportamiento del rastreo

- **Cada 60 segundos** el bot consulta la API de Jaha y te manda un resumen de los buses en camino.
- **Alerta inmediata 🔔** cuando algún bus está a ≤ 10 minutos de tu parada.
- El rastreo filtra automáticamente buses que ya pasaron tu parada.
- Para no saturar la API de Jaha, el intervalo es de 1 minuto (configurable en `bot.py` con `INTERVALO_RASTREO_SEG`).

---

## Ajustar parámetros en bot.py

```python
UMBRAL_ALERTA_MIN = 10      # Alerta cuando el bus está a ≤ N minutos
INTERVALO_RASTREO_SEG = 60  # Consultar cada N segundos
LINEAS_DISPONIBLES = ["187", "E1", "E2", "E3", "454"]  # Agregar más líneas acá
```

---

## Correr bot + web app juntos

```bash
# Terminal 1 — Web app
python app.py

# Terminal 2 — Bot de Telegram
TELEGRAM_TOKEN="tu_token" python bot.py
```

O con un `Procfile` para Heroku/Railway:
```
web: gunicorn app:app
worker: python bot.py
```
