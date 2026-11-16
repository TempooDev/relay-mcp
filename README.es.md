# relay-mcp

**Haz que tus agentes de IA deleguen trabajo en otros modelos.** `relay-mcp` es un servidor [MCP](https://modelcontextprotocol.io) que da a Claude Code, Antigravity, Gemini CLI y cualquier otro cliente MCP una única herramienta para delegar tareas en modelos más baratos, rápidos o locales, mediante una cola de tareas persistente.

> [English](README.md) · Español

```
Claude Code ──┐                          ┌─► Gemini (vía Antigravity CLI)   "light"
Antigravity ──┼─► relay-mcp ─ cola ──────┼─► Ollama / LiteLLM / cualquier API "local"
Gemini CLI  ──┘   (un servidor, SQLite)  ├─► Claude Sonnet                  "heavy"
                                         └─► Claude Fable / Opus            "max"
```

## Por qué

- **Gasta tokens premium solo donde importa.** Tu mejor modelo planifica; el trabajo masivo (resúmenes, clasificación, extracción, primeros borradores) va a algo más barato o gratuito.
- **Mantén el material sensible en tu máquina.** Con el nivel `local`, nada sale de tu ordenador.
- **Una sola configuración para todos los clientes.** Claude Code y Antigravity comparten servidor, cola y configuración.
- **Las tareas largas no chocan con los timeouts de MCP.** Devuelven un id y recoges el resultado después, desde cualquier cliente.

## Características

- Niveles (*tiers*) en lugar de nombres de modelo: pides `light` o `heavy` y cambias lo que hay detrás en un solo archivo.
- Alternativas ordenadas por nivel: si el primer destino falla o no tiene clave, responde el siguiente.
- Dos tipos de backend: **HTTP compatible con OpenAI** (Ollama, LiteLLM, vLLM, LM Studio, OpenAI...) y **CLI** (`claude -p`, `agy -p` o cualquier comando no interactivo, con la cuenta que ya tienes iniciada).
- Cola SQLite persistente: estado, resultados, cancelación e historial que sobrevive a reinicios.
- Pasa **rutas** de archivo, no contenido: el servidor lee los archivos y no pagas esos tokens dos veces.
- Transcripción de audio opcional con cualquier endpoint `/audio/transcriptions` compatible con OpenAI.
- Seguro por defecto: solo localhost, lista de carpetas permitidas, nombres protegidos y sin sobrescribir. Ver [Seguridad](#seguridad).

## Requisitos

- Python 3.11+ y [uv](https://docs.astral.sh/uv/) (recomendado).
- Al menos un backend: [Ollama](https://ollama.com), una clave de API o un CLI como `claude` o `agy`.

## Inicio rápido

```bash
git clone https://github.com/OWNER/relay-mcp
cd relay-mcp
uv sync
uv run relay-mcp init        # crea ./relay.toml
```

Edita `relay.toml` con los modelos que realmente tienes y arranca el servidor, dejándolo abierto:

```bash
uv run relay-mcp
# relay-mcp 0.1.0 using ./relay.toml - http://127.0.0.1:8765/mcp
```

### Conectar tus clientes

**Claude Code**

```bash
claude mcp add --transport http --scope user relay http://127.0.0.1:8765/mcp
```

**Antigravity**: añade a `~/.gemini/config/mcp_config.json` (o usa `agy mcp add`):

```json
{ "mcpServers": { "relay": { "url": "http://127.0.0.1:8765/mcp" } } }
```

**Gemini CLI**: añade a `~/.gemini/settings.json` (revisa la documentación de MCP de tu versión si la clave difiere):

```json
{ "mcpServers": { "relay": { "httpUrl": "http://127.0.0.1:8765/mcp" } } }
```

**Otros clientes**: apúntalos a `http://127.0.0.1:8765/mcp` (Streamable HTTP). Los que solo lanzan comandos locales pueden usar stdio, con un proceso por cliente:

```json
{ "mcpServers": { "relay": { "command": "uv", "args": ["--directory", "/ruta/a/relay-mcp", "run", "relay-mcp", "--transport", "stdio"] } } }
```

Reinicia el cliente (o abre una sesión nueva) y comprueba que aparece (`/mcp` en Claude Code).

## Uso

No llamas a las herramientas tú: se lo pides a tu agente.

> *"Usa relay para resumir `C:\notas\reunion.txt` en cinco puntos y guárdalo en `C:\notas\resumen.md`."*
>
> *"Delega la clasificación de estos tres archivos en el nivel `local` y dime solo las categorías."*
>
> *"Usa relay con el nivel `max` para revisar este diseño."*
>
> *"Lanza ese resumen en segundo plano y dame el id de la tarea."*

Los agentes suelen resolver solos las tareas pequeñas. Si quieres que siempre deleguen cierto trabajo, dilo en el prompt o en tu `CLAUDE.md` / `AGENTS.md`.

### Herramientas

| Herramienta | Qué hace |
|---|---|
| `delegate` | Envía una tarea de texto a un nivel, con archivos adjuntos, prompt de sistema y `save_to` opcionales. Espera hasta `wait_seconds` (45 por defecto) y, si sigue en marcha, devuelve un id. |
| `run_agent` | Ejecuta un [agente](docs/agents.md) predefinido: un archivo Markdown con prompt de sistema y modelo. Mismas opciones que `delegate`. |
| `list_agents` | Los agentes disponibles y el modelo de cada uno. |
| `clear_thread` | Olvida el historial de un hilo de conversación. |
| `task_status` | Estado y resultado de una tarea; puede esperar a que termine. |
| `list_tasks` | Tareas recientes, de la más nueva a la más antigua. |
| `cancel_task` | Cancela una tarea pendiente o en curso. |
| `list_tiers` | Los niveles y los modelos de cada uno. |
| `list_models` | Lista en vivo de lo que ofrece cada backend, más el mapa de niveles. |
| `transcribe` | Transcribe un archivo de audio (requiere la sección `[transcription]`). |

Estados: `pending`, `running`, `completed`, `failed`, `cancelled`, `interrupted` (el servidor se reinició a mitad de tarea).

### Agentes, contexto compartido e hilos

Define agentes como archivos Markdown, asigna un modelo a cada uno y deja que construyan sobre el trabajo de los demás, incluso entre proveedores distintos:

```markdown
<!-- agents/explorer.md -->
---
description: Mapea los archivos que le adjuntes e informa de qué hay dónde.
model: claude/haiku
---
Eres un explorador de código. Trabaja solo con el material que recibes...
```

> *"Ejecuta el explorer sobre `src/`, luego el researcher con `context_from` la tarea del explorer, y sigue en el hilo `review-1`."*

- `context_from` pasa el resultado de tareas anteriores a una nueva, sea cual sea el modelo que las produjo. Tu agente principal no copia el texto.
- `thread` mantiene una conversación de varios turnos, y un turno posterior puede usar otro modelo.
- Hay ejemplos para copiar en [`examples/agents`](examples/agents); la guía completa está en [docs/agents.md](docs/agents.md) (en inglés).

Los agentes de relay solo trabajan con texto: ven lo que les adjuntas y lo que produjeron tareas anteriores, y no pueden leer otros archivos ni ejecutar comandos.

### Ver qué hace cada modelo

Tu agente resume lo que vuelve de una tarea delegada, y a veces esconde detalle. Para ver lo real, abre una segunda terminal (o un panel de tmux o [herdr](https://github.com/ogulcancelik/herdr)) y sigue la cola en directo:

```bash
relay-mcp watch          # anuncia cada tarea y la imprime completa al terminar
relay-mcp tasks          # lista las tareas recientes
relay-mcp show 4b6d      # una tarea completa (basta el inicio del id)
```

El visor lee la base de datos directamente y nunca escribe, así que funciona con el servidor en marcha y también después de pararlo. Por defecto muestra los primeros 200 caracteres de cada prompt. Para guardar el texto **completo** enviado al modelo (prompt de sistema y archivos adjuntos incluidos), pon `store_prompts = true` en `[server]`. Está desactivado por defecto porque guarda sin cifrar el contenido de tus archivos; el cliente MCP nunca lo recibe de vuelta.

## Configuración

`relay-mcp` busca la configuración en este orden: `--config RUTA`, la variable `RELAY_MCP_CONFIG`, `./relay.toml` y `~/.config/relay-mcp/config.toml` (`%APPDATA%\relay-mcp\config.toml` en Windows). El [ejemplo comentado](src/relay_mcp/relay.example.toml) es la referencia; lo esencial:

```toml
[backends.ollama]                       # cualquier endpoint compatible con OpenAI
base_url = "http://localhost:11434/v1"

[backends.claude]                       # un CLI en modo no interactivo
type = "cli"
command = "claude"
args = ["-p", "--model", "{model}", "--tools", "", "--strict-mcp-config"]

[tiers]                                 # "backend/modelo"; responde el primero que pueda
light = ["ollama/llama3.2"]
heavy = ["claude/sonnet"]
```

Más detalle en [docs/configuration.md](docs/configuration.md) (en inglés).

## Seguridad

`relay-mcp` lee archivos y envía su contenido al modelo del nivel elegido. Tenlo presente antes de exponerlo a un agente que lea contenido no confiable.

- **Solo localhost y sin autenticación.** Quien alcance el puerto puede usarlo. No lo expongas en una interfaz pública.
- **Lista de carpetas permitidas.** Solo se leen o escriben archivos bajo `allowed_roots` (por defecto, tu carpeta personal). Redúcela a tus proyectos.
- **Nombres protegidos** como `.ssh`, `.aws`, `.env`, `*.pem` o `id_rsa*` se rechazan siempre.
- **Sin sobrescribir y solo formatos de texto.** `save_to` solo crea archivos nuevos y únicamente con extensiones permitidas (`.md`, `.txt`, `.json`, `.csv`...). No se pueden escribir scripts ni ejecutables, ni siquiera con trucos de Windows como `run.bat.` o `run.bat::$DATA`.
- **A prueba de navegador.** El endpoint HTTP rechaza cabeceras `Host` y `Origin` ajenas (protección contra DNS rebinding), así que una web no puede alcanzarlo a través de tu navegador.
- **Los CLI de tipo agente** (como `agy`) son agentes completos: un archivo adjunto podría contener instrucciones dirigidas a ellos. Prefiere backends de tipo `openai` para contenido no confiable.
- **La privacidad depende del nivel.** `local` se queda en tu máquina; `light`, `heavy` y `max` envían el contenido al proveedor que haya detrás. Las tareas y resultados se guardan sin cifrar en SQLite (se borran tras `retention_days`).

Para informar de vulnerabilidades, ver [SECURITY.md](SECURITY.md).

## Problemas frecuentes

| Síntoma | Causa / solución |
|---|---|
| `error while attempting to bind on address ... 8765` | Ya hay otra instancia en marcha. Páralo o usa `--port`. |
| `/mcp` en el navegador da `406 Not Acceptable` | Normal: el endpoint solo habla MCP. |
| Un nivel falla con `skipped (... not found on PATH)` | Ese CLI no está instalado o no está en el PATH; se prueba el siguiente destino. |
| `skipped (environment variable X is not set)` | Exporta la variable antes de arrancar el servidor. |
| El nivel de `agy` falla con prompts grandes | `agy` recibe el prompt como argumento y Windows limita su tamaño. Usa otro backend para entradas grandes. |
| El cliente no muestra las herramientas | Reinicia el cliente o abre una sesión nueva y comprueba que el servidor está en marcha. |

## Desarrollo

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Ver [CONTRIBUTING.md](CONTRIBUTING.md).

## Licencia

[MIT](LICENSE)
