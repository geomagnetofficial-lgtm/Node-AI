# DECISIONES.md — suposiciones y decisiones tomadas

## Alcance
- **Fase 6 (interfaz y temas) aplazada por completo**: me pediste ignorarla hasta tener `Interfaz.pdf`. No he tocado
  `ThemeMode.DARK`, ni creado `tema.json` (la carpeta `usuario/` ya está lista para él). Sí hice, por petición posterior:
  **varios idiomas** y **eliminar el panel derecho** (todo pasa a Configuración).
- Las partes de UI de las fases 2–5 (interruptor de API key, campo de rondas, selector de pasos, selector de modelos,
  biblioteca) se integraron en un único paso de UI, porque la reestructuración de Configuración las afecta a todas. La
  lógica de cada fase se probó y se confirmó en verde antes de ese paso.

## Fase 1 — carpeta de usuario
- `usuario/` cuelga de `app_dir()` (respeta `NODEAI_HOME`). La migración solo ocurre con la ruta por defecto (no con rutas
  explícitas, que usan los tests) y solo si `usuario/config.json` aún no existe.
- El `config.json` antiguo **no se borra** nunca en silencio; se crea `config.json.bak` (si no existía) y se avisa con un aviso.

## Fase 2 — API keys
- Campo nuevo por IA `save_key` (False por defecto). `api_key` se **excluye del JSON** (la propia clave del campo) cuando
  es False. `api_key` además tiene `repr=False` para que no salga en trazas.
- Configuración antigua con claves: se marcan `save_key=True` al cargar (nada se pierde) y se muestra **un único aviso**
  (`keys_notice_shown`) con «Mantener guardadas» / «Quitar del disco». «Quitar» también **borra** `config.json` y
  `config.json.bak` antiguos (lo dice el aviso), porque si no las claves seguirían en esas copias. Si se cierra sin elegir,
  el aviso reaparece.
- Un `config.json` editado a mano con `api_key` y `save_key: false` carga la clave en memoria y la deja de escribir en el
  siguiente guardado (se respeta lo explícito).
- Desactivar el interruptor guarda al instante (no espera al debounce) para que la clave salga del disco enseguida.

## Fase 3 — rondas y agente
- `MAX_ROUNDS = 50` y aviso al pasar de `ROUNDS_WARN = 5`. Por encima de 50 se ajusta a 50 con aviso (no se rechaza).
  El orquestador también aplica `clamp_rounds` por si el JSON trae otro valor.
- El campo de rondas valida al escribir (aplica solo valores válidos) y, al salir del campo o pulsar Enter, restaura el
  último válido con aviso si es inválido. Solo dígitos ASCII (rechaza `2.5`, `-1`, `1e3`, números árabes, etc.).
- Progreso: «ronda r/R» y «hechas/total» con total = IAs activas × rondas; las pestañas de la mesa muestran «ronda n» si
  hubo más de una.
- **Detección de bucles solo en modo sin límite** (en modo con límite sigue el aviso a las 3 repeticiones y el tope de pasos,
  sin cambios): 5 acciones idénticas **con el mismo resultado** seguidas, u 8 acciones de GUI (clic, teclado, scroll…) sin
  cambio visible. `wait`, `screenshot` y `move` no cuentan como «sin efecto». Ambos umbrales son ajustables
  (`loop_repeat`, `stall_limit`). Limitación: un modelo que alterna comandos distintos sin progreso real no se detecta
  como bucle; ahí actúa Detener.
- Un **clic sin efecto visible no se repite solo** (podría disparar dos veces un interruptor). En su lugar se activa una
  rejilla más densa (cada 50 px) y se le dice al modelo que corrija; la rejilla se apaga cuando un clic vuelve a tener efecto.
- `settle_s` pasa a ser la espera **mínima** (0 = no esperar nada) y `settle_max_s` (3 s) el máximo hasta estabilizar.
  Con un escritorio sin `wait_stable` (dobles de prueba) se conserva el sleep fijo anterior.
- Enfoque: `focus` fija una ventana objetivo y `type`/`key` la reenfocan si perdió el foco; el objetivo se olvida tras un
  `click` o `open` (para no devolver el foco a la fuerza tras un cambio intencional). `type`/`key` aceptan `window`
  opcional (ampliación del protocolo, compatible hacia atrás).

### Auditoría de `desktop.py` / `tools.py` — qué encontré
1. El modo DPI-aware se activaba dentro de `_pyautogui()`, **después** de la primera captura con `mss`: la primera captura
   podía salir en otra escala que el ratón (Windows con escalado). → Se activa antes de capturar (per-monitor v2 con
   respaldos).
2. La escala se guardaba como un único factor y se redondeaba sin límites: podía salir 1 px fuera del monitor. → Escala por
   eje + recorte al monitor + función pura `map_point` probada con orígenes negativos (monitor a la izquierda).
3. El índice de monitor no se validaba. → Respaldo al principal con nota al modelo, y aviso cuando hay varios monitores.
4. Espera fija tras cada acción (0,7 s): lenta si ya estaba lista y corta si cargaba. → Espera a que dos capturas
   consecutivas coincidan.
5. Nunca se comprobaba si la acción tuvo efecto. → Firma 160×90 en grises antes/después; el modelo recibe «Efecto visible»
   o «AVISO: la pantalla NO cambió».
6. El ratón podía no llegar donde se pidió (DPI/monitor) sin que nadie lo notara. → Se compara `pyautogui.position()`.
7. Teclear sin comprobar foco. → `ensure_focus` + informe de ventana activa tras teclear.
8. Errores escuetos («Falta el argumento 'x'»). → Se añade la firma de uso, sugerencias de teclas parecidas y rangos válidos.
9. La «detección de bucles» solo avisaba. → Ver arriba (modo sin límite).
10. No cambiado: `FAILSAFE` de pyautogui usa la esquina (0,0) del monitor **principal**; en varios monitores es esa esquina.
11. Límites: el parpadeo del cursor de texto puede contar como cambio mínimo; `active_window` necesita `xdotool` en Linux y
    no está disponible en macOS (se enfoca igualmente); `tests/test_agent_desktop_real.py` sigue omitido sin `tkinter`/pantalla,
    pero verifiqué la captura real, el mapeo y la estabilización con Xvfb a mano.

## Fase 4 — modelos
- `list_models(provider_key, api_key, base_url="", *, models_dir="")`: añadí `models_dir` como argumento por nombre para
  GGUF (la firma pedida no lo traía). Para GGUF devuelve **rutas completas** (es lo que se guarda en `gguf_path`).
- Selector = diálogo con búsqueda + «Actualizar lista» junto al campo (la lupa), no un desplegable embebido: el campo de texto
  se mantiene para el id manual. Para GGUF se conserva el desplegable «Modelo instalado» existente.
- La caché es solo en memoria y su clave usa un hash de la API key (la clave en claro no se guarda en la caché).
- No se filtra por «solo modelos de chat» (las listas de OpenAI incluyen embeddings, audio…): para eso está el buscador.

## Fase 5 — biblioteca
- Los **textos de `ROLES` y `DIRECTOR` se migraron idénticos** (comprobado byte a byte contra el `roles.py` original, y con
  tests). `ROLES`, `DIRECTOR` y `role_prompt` siguen existiendo en `roles.py` por compatibilidad.
- Esquema ampliado con campos opcionales: roles `ambito` (`mesa`|`director`) y, en los por defecto, `desde` (versión en que
  se añadió). El usuario guarda además `defaults_vista` (última versión por defecto que vio) para poder avisar de novedades.
- El Director tenía un desplegable «Rol» que **no hacía nada** (el orquestador usaba `DIRECTOR` fijo). Lo sustituí por
  «Rol del Director» (`director_role`, por defecto `director`, solo roles con ámbito `director`); el comportamiento por
  defecto es idéntico.
- Id desconocido o de ámbito equivocado → cae a `neutral` (mesa) o `director`. Borrar un rol personalizado no rompe las IAs que
  lo usaban.
- «Editar» un elemento por defecto **no crea la copia hasta pulsar Guardar** (cancelar no deja basura).
- Importar: conserva ids `usr-…` si no chocan; cualquier otro id se regenera con prefijo y se reasignan las referencias de
  rol; los ids que ya existen en la biblioteca del usuario se **omiten** (no se pisan). Exportar escribe solo lo del usuario.
- Archivo del usuario corrupto/ilegible: se ignora, se avisa y se guarda una copia `.corrupt` antes de poder sobrescribirlo.
- «Usar» un prompt **sobrescribe** el system prompt de la IA elegida (y su rol si el prompt trae uno de mesa válido).
- `NodeAi.spec`: `datas=[('src/data', 'src/data')]` para que el `.exe` incluya prompts e idiomas. No lo he empaquetado.

## Idiomas
- Español, English, Português, Français, Deutsch. Las **claves son los textos en español**; sin traducción se muestra el
  español. `tests/test_i18n.py` falla si falta una traducción o un `{marcador}` no coincide.
- Usuarios existentes sin campo `language` se quedan en español; una instalación nueva usa el idioma del sistema si está
  soportado.
- Las traducciones las he escrito yo: conviene que alguien nativo las repase.
- Los textos de los roles/prompts por defecto siguen en español (solo se traducen sus nombres/títulos/categorías); los mensajes
  ya mostrados en el chat no se retraducen al cambiar de idioma.
- Los mensajes de validación de la biblioteca (p. ej. «'titulo' es obligatorio») se muestran tal cual, en español.

## Configuración sin panel derecho
- Configuración = lista de categorías a la izquierda (ventana ancha) o fila de botones desplazable (estrecha): General,
  Director, Mesa de IAs, Agente, Biblioteca, Local y hardware. El interruptor «activar agente» solo está en Agente (no hay
  atajo en el chat). El interruptor de activar/desactivar cada IA pasó al inicio de su fila en «Mesa de IAs».
- Se corrigió un fallo que apareció al mover el hardware de pestaña: la caja de hardware no se refrescaba tras detectar.

## Recomendación de `.gitignore` (no lo he tocado)
`build/`, `dist/` y `__pycache__/` (este último ya está). `usuario/` vive en la carpeta de datos, no en el repo.
