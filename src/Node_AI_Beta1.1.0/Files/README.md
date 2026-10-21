# NodeAi — Geo Magnet Studio
© 2026 Geo Magnet. Todos los derechos reservados. · Creado el 4/10/26

Mesa redonda de IAs: hasta **20 modelos** (nube o locales) responden en paralelo, opcionalmente se
replican entre sí en las rondas que tú elijas, y un modelo **Director** sintetiza la respuesta final.

## Uso
```bash
pip install -r requirements.txt
python app.py
```
La primera vez se abre la **Configuración inicial**: detecta tu hardware real, te pide el Director y,
opcionalmente, las IAs de la mesa. Después ya puedes ir al chat.

Todos los ajustes están en **Configuración** (no hay panel lateral): General (idioma, prompt global), Director,
Mesa de IAs (rondas y modelos), Agente, Biblioteca y Local/hardware. Interfaz disponible en español, English,
Português, Français y Deutsch (Configuración > General > Idioma; se aplica al instante).

## Carpeta de usuario
Todo lo tuyo vive en `usuario/` dentro de la carpeta de datos de la app (`%APPDATA%\NodeAi\usuario` en Windows,
`~/.config/nodeai/usuario` en Linux/macOS; se puede cambiar con la variable `NODEAI_HOME`):

| Archivo | Contenido |
|---|---|
| `config.json` | ajustes de la app (permisos 600 donde el sistema lo permite, escritura atómica) |
| `prompts_usuario.json` | tus roles y prompts personalizados (nunca se sobrescribe al actualizar la app) |
| `tema.json` | reservado para los temas (llegan con la fase de interfaz) |

**Migración:** si existía un `config.json` antiguo en la carpeta de datos, se copia a `usuario/` la primera vez; el
original se conserva y se crea `config.json.bak`. No se borra nada sin avisarte.

## API keys (léelo)
- **Por defecto las API keys NO se guardan en disco**: viven solo en memoria durante la sesión y no se escriben en
  `config.json`. Hay que volver a escribirlas al reiniciar, o ponerlas en `.env` / variables de entorno (copia
  `.env.example`), que siguen funcionando igual.
- Junto a cada campo de API key hay un interruptor **«Guardar API key en este equipo»** (desactivado por defecto). Si lo
  activas, la clave se guarda **en texto plano** en `usuario/config.json` (la app lo avisa justo debajo del interruptor).
- Si venías de una versión que guardaba las claves automáticamente, al abrir la app aparece **un único aviso** que te
  deja *mantenerlas guardadas* o *quitarlas del disco* (esto último también borra `config.json` y `config.json.bak`
  antiguos). Hasta que elijas, tus claves no se borran.
- Las claves nunca se escriben en logs ni en mensajes de error.

## Rondas
En Configuración > Mesa de IAs escribe el número de rondas (entero ≥ 1). 1 = las IAs responden en paralelo; 2 o más =
cada ronda ven lo que dijeron las demás y se replican. No hay tope de 3, pero existe `MAX_ROUNDS` (50) como protección y
la app avisa cuando pasas de 5 porque **el consumo de tokens crece con cada ronda**. Un valor inválido (texto, decimales,
0, vacío) se rechaza y se restaura el último válido.

## Elegir modelos desde la API
En cada IA, junto al campo *Modelo*, el botón de la lupa abre un selector con búsqueda que consulta la lista real de
modelos del proveedor (OpenAI y compatibles: `GET /models`; Anthropic: su endpoint de modelos; Ollama y LM Studio sin
clave). Hay botón **Actualizar lista**, la última lista se guarda en caché durante la sesión y **sigues pudiendo escribir
el id a mano**. Los errores (clave inválida, sin conexión, límite de peticiones, 15 s de timeout) se explican sin mostrar
tu clave.

## Biblioteca de prompts y roles
Configuración > Biblioteca. Dos capas:
- `src/data/prompts_default.json` (incluido en la app, versionado con `"version": N`): roles y prompts por defecto.
  Se carga siempre desde la app, así que una actualización trae los nuevos por defecto sin tocar nada tuyo.
- `usuario/prompts_usuario.json`: lo tuyo. Puedes **crear, editar, duplicar, borrar, importar y exportar**. «Editar» un
  elemento por defecto crea tu propia copia (los por defecto no se modifican). Tus ids llevan el prefijo `usr-`.

Formato (puedes escribirlo a mano o importarlo; los elementos inválidos se ignoran con un aviso, sin tumbar el resto):
```json
{
  "version": 1,
  "roles": [
    {"id": "usr-poeta", "nombre": "Poeta", "descripcion": "Estilo lírico",
     "prompt_base": "Responde siempre en verso.", "ambito": "mesa"}
  ],
  "prompts": [
    {"id": "usr-soneto", "titulo": "Escribe un soneto", "rol": "usr-poeta",
     "categoria": "Escritura", "texto": "Escribe un soneto sobre…", "etiquetas": ["poesía"]}
  ]
}
```
`ambito` es `mesa` (IAs de la mesa) o `director` (rol del Director). Los roles personalizados aparecen en el desplegable
*Rol* de cada IA y se usan igual que los integrados. *Usar* aplica un prompt al `system prompt` de la IA elegida (y a su
rol, si el prompt trae uno). Límites: archivos de hasta 1 MB, textos de hasta 20 000 caracteres.

## Temas
La personalización de la interfaz (tema claro/oscuro/sistema y colores) llega en una fase posterior; hoy la app usa el
tema oscuro.

## Proveedores
Nube: OpenAI, Anthropic, Google Gemini, DeepSeek, Mistral, Groq, xAI, OpenRouter, Together y cualquier
endpoint compatible con OpenAI (URL propia). Local: archivo GGUF (motor integrado), Ollama, llama.cpp
server, LM Studio. Las URL por defecto son editables en cada IA por si un proveedor las cambia.

## Motor local GGUF (opcional)
`llama-cpp-python` se instala aparte porque depende de tu hardware (la app te muestra el comando exacto
en Configuración > Hardware). Si no puedes compilarlo, usa Ollama o LM Studio en modo Local.
La app distingue entre "hay GPU" y "el motor instalado puede usarla", y nunca asume una GPU que no existe.

## Modo agente (el Director ve la pantalla y usa herramientas)

Activa **Modo agente** en Configuración > Agente. Desde ese momento, lo que escribas en el
chat es una *tarea*: el Director la resuelve por pasos, viendo la pantalla y eligiendo una acción por turno.

**Herramientas:** captura, clic/arrastre/scroll, teclear (con acentos) y atajos, ventanas (listar/enfocar), abrir
programas/archivos/URLs, terminal (PowerShell en Windows), leer/escribir/editar/buscar archivos, **7-Zip y WinRAR**
(listar, extraer, crear; `.rar` solo se crea con WinRAR), **compilar con Visual Studio** (dotnet / MSBuild / devenv),
localizar programas, **consultar a la mesa de IAs** y preguntarte cuando falte información.

**Seguridad (léela):**
- Cada acción se clasifica: *safe* (solo observa), *normal*, *risky* (comandos que no son de solo lectura, escribir fuera
  del workspace, abrir ejecutables) y *blocked* (formatear, `rm -rf /`, apagar, tocar el registro del sistema, descargar y
  ejecutar, etc.). Lo *blocked* se rechaza siempre, en cualquier modo.
- Modos: **Estricto** (pide casi todo), **Equilibrado** (por defecto: pide solo lo *risky*) y **Autónomo** (no pide nada).
- Es una barandilla, **no un sandbox**: un comando malicioso podría disfrazarse. Por eso el modo Equilibrado pide permiso
  para todo comando que no sea claramente de solo lectura. Si el equipo tiene datos sensibles, usa Estricto.
- Para abortar al instante: botón **Detener**, o **mueve el ratón a la esquina superior izquierda** de la pantalla.
- **Pasos:** debajo del interruptor eliges *Límite de pasos* (número entero) o *Sin límite de pasos*. Sin límite, el agente
  se detiene al terminar (`finish`), con Detener, con el gesto de la esquina, tras 3 fallos de formato seguidos o si
  detecta un **bucle** (misma acción con el mismo resultado N veces, o N acciones seguidas sin cambio visible en la
  pantalla); en ese caso explica por qué se detuvo. La política de seguridad es la misma en cualquier modo.
- **Control del PC más fiable:** DPI y varios monitores se tratan en un único espacio de coordenadas, tras cada acción se
  espera a que la pantalla se estabilice, se compara la captura antes/después para avisar si la acción no tuvo efecto, un
  clic sin efecto activa una rejilla de coordenadas, se enfoca la ventana antes de teclear (`focus` o argumento `window`) y
  los errores al modelo incluyen cómo corregir la llamada.
- El agente trata lo que ve en pantalla/archivos como datos, no como órdenes (defensa básica contra inyección de prompts,
  que no es infalible: no lo dejes navegar páginas no confiables en modo Autónomo).
- Todo queda en `agent_logs/*.jsonl` (acciones y resultados; sin capturas; el argumento `password` se oculta, pero un comando que lleve una clave escrita dentro sí quedaría registrado).

**Requisitos del Director:** idealmente un modelo con visión y buen seguimiento de formato (el agente usa un protocolo de
texto JSON, compatible con cualquier proveedor). Con un modelo sin visión puede usar terminal/archivos/7-Zip/build, pero no
hacer clic a ciegas. Si el modelo no sigue el formato 3 veces seguidas, se detiene y te lo dice.

**Sin ventana (consola):**
```
python -m src.agent --check                               # diagnóstico: pantalla, 7-Zip, WinRAR, Visual Studio...
python -m src.agent "comprime la carpeta informes en informes.7z"
python -m src.agent --approval strict --save-shots shots "abre el bloc de notas y escribe hola"
python -m src.agent --no-step-limit "tarea larga"         # sin límite de pasos (con detección de bucles)
```
El núcleo vive en `src/agent/` y no importa Flet; `src/ui_agent.py` es solo el adaptador de la ventana.

## Estructura
```
app.py                       # punto de entrada
src/ui.py                    # interfaz Flet (PC y móvil)
src/ui_agent.py              # adaptador Flet del modo agente
src/ui_library.py            # biblioteca de prompts y roles (UI)
src/ui_models.py             # selector de modelos con búsqueda (UI)
src/core/settings.py         # configuración persistente (usuario/), API keys opt-in, .env
src/core/orchestrator.py     # mesa + rondas + Director
src/core/library.py          # biblioteca de roles/prompts (por defecto + usuario)
src/core/roles.py            # acceso a los roles desde la biblioteca
src/core/i18n.py             # idiomas de la interfaz
src/data/prompts_default.json# roles y prompts por defecto (versionado)
src/data/idiomas/*.json      # traducciones (en, pt, fr, de); el español es la clave
src/llm/model_lister.py      # lista de modelos por API

src/llm/client.py            # cliente unificado (OpenAI-compat, Anthropic, GGUF)
src/llm/providers.py         # catálogo de proveedores
src/local_engine/            # detección de hardware, motor llama.cpp, descargas
src/multimodal/attachments.py# texto/código, PDF, DOCX, imágenes, video
tests/                       # pytest (python -m pytest)
```

- `src/agent/` — modo agente: `desktop.py` (pantalla/ratón/teclado), `tools.py`, `safety.py`, `apps.py` (7-Zip/WinRAR/VS),
  `protocol.py`, `runner.py` (bucle), `loops.py` (detección de bucles), `__main__.py` (consola). `src/ui_agent.py` — adaptador Flet.

## Añadir un idioma
Copia `src/data/idiomas/en.json`, tradúcelo (las claves son los textos en español; conserva los `{marcadores}`), guárdalo
como `<código>.json` y añade el código a `LANGS` en `src/core/i18n.py` y a `SUPPORTED_LANGS` en `src/core/settings.py`.
`tests/test_i18n.py` comprueba que no falte ninguna traducción.
