# Programación y notificación (fase 8)

Corrida nocturna sin intervención: el comando `swing nightly` es el punto de
entrada que agenda el Programador de tareas, y la notificación es opcional.

## Corrida nocturna

`swing nightly` corre las dos funciones de una sola pasada:

1. `screen` (siempre): screener de compra; deja `state/candidates.json`.
2. `review` (si hay export de XTB en `posiciones/` o `--portfolio`): revisión de
   posiciones.

Escribe un resumen en `output/nightly_<fecha>.md` y devuelve un código de salida
combinado (`config/datos` manda sobre `IA`, y esta sobre `hallazgos`):

| Código | Significado |
|---|---|
| 0 | corrida limpia |
| 2 | hallazgos o datos incompletos (fail-closed: no opera con datos malos) |
| 3 | fallo de la IA (la recomendación determinista igual se escribió) |
| 1 | error de configuración o de datos irrecuperable |

```powershell
swing nightly                 # determinista
swing nightly --llm --web-search   # con la opinion de la IA
swing nightly --no-notify     # sin aviso, solo el resumen en disco
```

Opciones: `--top` (candidatos), `--portfolio` (export explícito), `--out-dir`
(dónde escribir el resumen).

## Horario

El motor no puede reaccionar intradía: toda decisión se toma después del cierre de
EE. UU. El margen (`data.min_minutes_after_close`, 90 min por defecto) es lo que
garantiza que la vela diaria ya está completa (D3).

`swing schedule` calcula la hora local en `America/Santiago` equivalente al cierre
de NYSE (16:00 ET) más el margen e imprime el comando `schtasks` listo:

```powershell
swing schedule                # diario
swing schedule --weekly       # además la variante de los domingos
swing schedule --llm          # incluye --llm --web-search en el comando
```

Ejemplo de salida y alta de la tarea (PowerShell):

```powershell
swing schedule
schtasks /Create /TN "Swing nightly" /SC DAILY /ST 18:30 /TR "\"C:\ruta\swing.exe\" nightly" /F
```

> **Horario de verano.** Chile y EE. UU. cambian de hora en fechas distintas, así
> que la hora local del disparo se corre sola en algunos tramos del año. Revisá el
> horario en **marzo** y **septiembre** y volvé a crear la tarea si hace falta.

La tarea no requiere privilegios especiales: corre en la sesión del usuario y usa
su entorno (incluidas las variables y `.secrets/`).

## Notificación (opcional)

Sin canales configurados no pasa nada: el resumen queda en disco. Los canales se
activan por variable de entorno o por un archivo en `.secrets/` (ignorado por git).

### Webhook genérico

Hace `POST` JSON `{"title": ..., "body": ...}`. Sirve para Slack, Discord, n8n o
cualquier endpoint propio.

```powershell
$env:SWING_NOTIFY_WEBHOOK = "https://hooks.example.com/..."
# o .secrets/notify_webhook.txt
```

### Telegram

```powershell
$env:SWING_TELEGRAM_BOT_TOKEN = "123456:ABC..."
$env:SWING_TELEGRAM_CHAT_ID   = "123456789"
# o .secrets/telegram_token.txt y .secrets/telegram_chat_id.txt
```

El envío es **best-effort**: si un canal falla, se reporta el error y la corrida
sigue. Un fallo de red al notificar nunca tumba una corrida que ya escribió sus
informes.

## Qué queda pendiente

- La **puerta de validación** (fase 3) sigue sin correrse sobre datos reales: el
  proyecto está **NO VALIDADO**. Programar la corrida no vuelve la estrategia
  segura; solo la automatiza.
- La verificación de broker (`docs/VERIFICACION_BROKER.md`) es bloqueante para
  operar con dinero real.
