# Drive Image Processor

Aplicación para el procesamiento automatizado
de fotografías de productos.

## Objetivo

Transformar imágenes mediante:

- Eliminación automática del fondo.
- Fondo blanco (predeterminado) o transparente.
- Centrado del producto sin deformarlo.
- Resolución de 400x400 píxeles (configurable).
- Conservación de los originales.
- Exportación a PNG.

## Uso

```
python -m image_processor.batch_cli --input ".\inputs" --output ".\outputs"
python -m image_processor.batch_cli --input ".\inputs" --output ".\outputs" --background transparent --width 800 --height 600
```

- `--background`: `white` (RGB, fondo `#FFFFFF`) o `transparent` (RGBA).
- `--width` y `--height`: entre 64 y 6000 píxeles, con un máximo
  de 16 000 000 píxeles en total. Por defecto, 400 x 400.

Origen y destino pueden ser la misma carpeta.

## Nombres de los resultados

Todos los destinos del lote se planifican antes de procesar, en
un orden estable (alfabético, sin distinguir mayúsculas) que no
depende del orden en que el sistema de archivos lista la carpeta.

Un nombre se compone de bloques, siempre en este orden:

```
TRANSFORMADA_<nombre base>[__esc][__src-<extensión>][__<ancho>x<alto>_<fondo>].png
```

| Situación | Ejemplo de resultado |
|---|---|
| Original único, variante predeterminada | `TRANSFORMADA_producto.png` |
| Original único, otra variante | `TRANSFORMADA_producto__800x600_transparent.png` |
| Varios originales con el mismo nombre base | `TRANSFORMADA_producto__src-jpg.png` |
| Varios originales, otra variante | `TRANSFORMADA_producto__src-jpg__800x600_transparent.png` |
| Nombre de original que contiene `__` | `TRANSFORMADA_producto%5F%5F800x600%5Ftransparent__esc.png` |

- **Separador reservado `__`:** los bloques del sistema empiezan con
  `__`. Un nombre base ordinario (sin `__`) se usa literal y conserva
  `TRANSFORMADA_<nombre>.png`, así que los resultados previos se siguen
  reconociendo.
- **Codificación:** si el nombre base contiene `__`, se codifica
  (`%` como `%25` y `_` como `%5F`) y se marca con `__esc`. Así un
  original llamado `producto__800x600_transparent.jpg` o
  `producto__src-jpg.png` nunca puede tomar el nombre de una variante
  o de otro original: cada combinación de original y variante tiene un
  nombre propio, también entre ejecuciones sucesivas.
- **Sufijo `__src-<extensión>`:** se agrega cuando dos o más
  originales comparten nombre base (`producto.jpg`, `producto.png`,
  `producto.webp`). Usa la extensión en minúsculas (`jpg`, `jpeg`,
  `png`, `webp`).
- **Sufijo de variante `__<ancho>x<alto>_<fondo>`:** identifica las
  dimensiones y el fondo, y va siempre al final. Cada variante es un
  archivo independiente.
- **Comparación de nombres:** sin distinguir mayúsculas y en forma
  Unicode NFC, como en Windows. `Producto.jpg` y `producto.png`
  comparten nombre base.
- **Resultados existentes:** un PNG existente nunca se sobrescribe.
  Solo se omite si el manifiesto lo verifica (ver la sección
  siguiente); la existencia del archivo no basta.
- **Resultados históricos ambiguos:** si aparece un segundo original
  con el mismo nombre base, el histórico `TRANSFORMADA_producto.png`
  no se atribuye a ninguno. Se conserva intacto, cada original
  genera su resultado `__src-` y el lote muestra una advertencia.
  Lo mismo ocurre si después queda un solo original, pero el destino
  todavía tiene resultados `__src-` de ese nombre base en cualquier
  variante.
- **Resultados de versiones anteriores:** antes de esta política, un
  original con `__` en el nombre se guardaba con el nombre literal, que
  hoy puede coincidir con el de otro original. Si un resultado
  existente pudo pertenecer a otro original actual, no se atribuye ni
  se sobrescribe: la imagen se informa como fallida y se indica que se
  mueva o renombre ese resultado. Cuando la variante del resultado
  heredado difiere de la pedida, el tamaño y el modo del PNG existente
  bastan para decidir.
- **Colisiones sin solución segura:** si dos originales reclaman el
  mismo nombre (por ejemplo, `café.jpg` escrito en formas Unicode NFC
  y NFD, que NTFS admite juntas), ninguno de los dos se procesa y cada
  uno se informa como fallido, con la indicación de renombrar. Las
  demás imágenes del lote se procesan normalmente.

- **Revisiones `__rev-<12 hex>`:** cuando el nombre base ya pertenece a
  otro resultado registrado (otro original con el mismo nombre, un
  cambio de contenido, otro modelo o una nueva versión del
  procesamiento), el nuevo resultado agrega este bloque al final. Se
  deriva del SHA-256 de (tipo e identificador del original, contenido,
  modelo, versión del procesamiento), así que es determinista. El
  resultado anterior se conserva.

## Manifiesto y reprocesamiento

Cada carpeta de destino tiene un manifiesto, `.transformada_manifest.json`,
que registra para cada PNG:

- el original: ruta relativa a la carpeta de destino, nombre, tamaño y
  SHA-256;
- el modelo, el ancho, el alto y el fondo;
- la versión del procesamiento (`PIPELINE_VERSION`), que se incrementa
  cuando cambia la semántica del resultado;
- el SHA-256 del PNG generado y su estado (`pending` o `completed`).

No contiene credenciales ni rutas absolutas, salvo que el original
esté en otra unidad de Windows.

La **identidad** de un resultado es: qué original es (tipo e
identificador; en local, su ruta relativa, sin distinguir mayúsculas),
su contenido (SHA-256), el modelo, las opciones y la versión del
procesamiento. Dos originales distintos nunca comparten un resultado,
aunque tengan los mismos bytes: por ejemplo, `marca_A/producto.jpg` y
`marca_B/producto.jpg` procesados hacia el mismo destino. En Google
Drive, el identificador será el `fileId`.

Política de cada imagen:

| Situación | Resultado |
|---|---|
| Misma identidad y el PNG coincide con su SHA-256 | Se omite sin cargar el modelo ni reescribir nada |
| El original cambió de contenido | Nueva revisión `__rev-`; el resultado anterior se conserva |
| Otro original con el mismo nombre base (otra carpeta) | Nueva revisión `__rev-` |
| Otro modelo o versión del procesamiento | Nueva revisión `__rev-` |
| Otro fondo o dimensiones | Otro nombre de variante |
| El PNG registrado ya no existe | Se regenera con el mismo nombre |
| El PNG registrado fue modificado | Fallo; nunca se sobrescribe |
| PNG existente sin registro (anterior al manifiesto) | **Sin verificar**: se conserva, no se da por válido y no se procesa |

Los resultados sin verificar requieren una decisión: revisarlos y
moverlos, o ejecutar con `--regenerate-unverified`, que genera una
versión verificada con nombre de revisión y conserva el histórico.
Nunca se adoptan automáticamente por su nombre. Como no se sabe con
qué modelo u original se generó un histórico, mientras exista bloquea
cada nueva combinación de modelo y contenido para ese nombre base: la
opción se necesita una vez por cada una (por ejemplo, para IS-Net y
para BiRefNet). Después, cada resultado queda verificado.

Recuperación ante interrupciones: cada imagen se registra en dos fases.

1. El PNG se codifica en memoria y se guarda una entrada `pending` con
   el SHA-256 de esos bytes exactos: la evidencia.
2. Se publican esos mismos bytes (sin sobrescribir).
3. La entrada pasa a `completed`.

Si el proceso se interrumpe entre la publicación y el registro, la
siguiente ejecución adopta el PNG **solo si su SHA-256 coincide con la
evidencia**; no basta con que tenga el tamaño o el formato esperados. Un
archivo ajeno o dañado en ese lugar queda sin verificar y se conserva
(con `--regenerate-unverified` se genera una versión verificada con
nombre de revisión). Si el PNG no llegó a publicarse, la imagen se
vuelve a procesar. Si el lote publica un PNG, pero no logra guardar su
registro completo, termina con el código 6 y la siguiente ejecución
lo recupera.

El manifiesto se guarda en un temporal y se sustituye de forma atómica;
si la escritura falla, el anterior queda intacto. Un manifiesto dañado
o de otra versión detiene el lote antes de procesar y nunca se
reinicia.

Códigos de salida de la CLI:

| Código | Significado |
|---|---|
| 0 | Todo procesado o verificado |
| 1 | Alguna imagen falló |
| 2 | Argumentos o carpetas inválidos |
| 3 | No se pudo inicializar el modelo |
| 4 | Sin fallos, pero hay resultados sin verificar |
| 5 | El manifiesto está dañado o no es compatible |
| 6 | Sin fallos, pero algún PNG publicado quedó con el registro pendiente |

Si se cumplen varias condiciones, prevalecen en este orden: 1, 6 y 4.

Limitaciones:

- No se admiten lotes concurrentes sobre la misma carpeta de destino.
- Cada ejecución calcula el SHA-256 de todos los originales elegibles.
- Agregar un segundo original con el mismo nombre base hace que el
  primero se vuelva a procesar con el sufijo `__src-` (su resultado
  anterior se conserva).
- La identidad local depende de la ruta del original relativa a la
  carpeta de destino. Si se mueve una carpeta respecto de la otra, o se
  renombra un original, se generan revisiones nuevas (nada se
  sobrescribe ni se atribuye a otro original).
- Una entrada `pending` creada por la versión anterior del manifiesto
  (sin SHA-256) no permite recuperar su PNG: queda sin verificar.

## Google Drive

La integración autoriza la cuenta, consulta metadatos (GD-01), descarga
imágenes a una carpeta local (GD-02), las procesa sin conexión (GD-03A)
y sube los PNG resultantes (GD-03B). Solo crea archivos nuevos: nunca
modifica, mueve ni elimina archivos de Drive, y nunca toca los
originales. El procesador local no depende de esta integración.

### Configuración de Google Cloud

1. En [Google Cloud Console](https://console.cloud.google.com/), cree o
   elija un proyecto.
2. En **APIs y servicios > Biblioteca**, habilite **Google Drive API**.
3. En **Google Auth Platform > Público** (pantalla de consentimiento
   OAuth), configure la aplicación como **Externa** en modo **Prueba** y
   agregue como usuario de prueba la cuenta de Google que va a usar.
4. En **Acceso a los datos**, agregue el scope
   `https://www.googleapis.com/auth/drive.readonly`. Para subir
   resultados (GD-03B), agregue también
   `https://www.googleapis.com/auth/drive.file`. No agregue
   `https://www.googleapis.com/auth/drive`: la aplicación no lo usa.
5. En **Clientes**, cree un cliente OAuth de tipo **Aplicación de
   escritorio** y descargue su JSON.
6. Guárdelo como `.secrets/credentials.json` en la carpeta desde la
   que ejecutará los comandos. `.secrets/` está excluida de Git.

La carpeta de secretos puede cambiarse con `--secrets-dir` o con la
variable de entorno `DRIVE_IMAGE_PROCESSOR_SECRETS_DIR`. Allí se
guardan también `token.json` (solo lectura) y `token_write.json`
(escritura con `drive.file`), con los tokens de acceso y de
actualización. Ninguno de estos archivos debe compartirse ni
confirmarse en Git.

### Comandos

```
python -m image_processor.drive_cli auth
python -m image_processor.drive_cli list-folders
python -m image_processor.drive_cli list-images --folder-id ID
python -m image_processor.drive_cli list-images --folder-id ID --originals-only --show-skipped
python -m image_processor.drive_cli logout
```

- `auth` abre el navegador para autorizar la cuenta (solo si no hay una
  autorización válida), guarda el token y verifica la conexión.
- `list-folders` muestra el ID, el nombre y la carpeta superior de cada
  carpeta accesible, y marca los nombres repetidos: las carpetas se
  identifican siempre por su ID.
- `list-images` lista las imágenes de una carpeta, sin entrar en
  subcarpetas, con su ID, nombre, tipo MIME, tamaño, fecha de
  modificación y MD5 de Drive. Recorre todas las páginas de resultados.
  Solo incluye archivos cuyo tipo MIME y extensión son JPEG, PNG o
  WebP. `--originals-only` excluye los resultados `TRANSFORMADA_`.
- `logout` elimina el token local. Para revocar el acceso en Google,
  use **Cuenta de Google > Seguridad > Aplicaciones de terceros**.

Los comandos de consulta nunca abren el navegador: si no hay una
autorización válida, indican que se ejecute `auth`.

Si `token.json` está dañado, los comandos de consulta lo rechazan y
`auth` inicia una nueva autorización. Solo si esta se completa, el
archivo dañado se conserva sin cambios en la misma carpeta privada
como `token.damaged-<fecha>-<aleatorio>.json` y se guarda el token
nuevo. Si la autorización se cancela, el archivo dañado queda como
estaba.

Códigos de salida: 0 correcto, 1 error inesperado de Drive, 2
argumentos inválidos, 3 autorización ausente, expirada, revocada o
insuficiente, 4 sin permiso, inexistente o no es una carpeta, 5 error
de red o límite de solicitudes después de los reintentos.

Los errores 429, 5xx, de cuota y de red se reintentan hasta 5 veces con
espera exponencial (1, 2, 4 y 8 segundos, o la indicada por Google).
Los errores de autorización y de permisos no se reintentan. La
verificación SSL nunca se desactiva.

Durante la autorización en el navegador (`auth`), los errores de
conexión, de tiempo de espera o de certificado SSL al intercambiar el
código por los tokens se informan con un mensaje claro (código 5). El
flujo interactivo no se reintenta automáticamente: basta con volver a
ejecutar `auth`.

### Descarga de imágenes (GD-02)

```
python -m image_processor.drive_cli download --folder-id ID --output downloads/prueba --limit 5
python -m image_processor.drive_cli download --folder-id ID --output downloads/prueba --originals-only --max-file-mb 25 --show-skipped
```

Descarga las imágenes JPEG, PNG y WebP que están directamente en la
carpeta (sin subcarpetas). `--limit` cuenta las imágenes elegibles
(descargadas o ya verificadas), en orden por nombre. El resumen
muestra las descargadas, las ya verificadas y omitidas, las rechazadas
por motivo, las fallidas y el total de bytes. Código de salida 6: alguna
imagen no se pudo descargar o verificar. Si fallan 3 imágenes seguidas
por la red, el lote se detiene (código 5).

**Nombres locales.** `<nombre saneado>--<fileId codificado><ext>`, por
ejemplo `producto--1~ab~c-_9.jpg`:

- El `fileId` siempre forma parte del nombre, así que dos archivos de
  Drive con el mismo nombre nunca chocan.
- Cada mayúscula del `fileId` se escribe como `~` y la minúscula
  (`A` -> `~a`). La codificación es reversible y no choca en sistemas
  que no distinguen mayúsculas.
- El nombre remoto se normaliza a NFC; se reemplazan los separadores
  (`/`, `\`), los caracteres no válidos en Windows y los de control; se
  evitan los nombres reservados (`CON`, `NUL`, `COM1`...); se quitan
  los puntos y espacios de los extremos, y se recorta a 80 caracteres.
  `--` y `__` se colapsan para no interferir con el separador ni con
  los nombres del procesador local.
- Si el contenido de un archivo cambió en Drive desde una descarga
  anterior, el nuevo se guarda como `...rev-<md5[:12]><ext>` y el
  anterior se conserva.

**Procedencia.** Cada imagen tiene un registro en
`.drive/<nombre local>.json` con el `fileId` exacto (sin normalizar),
el nombre original, el tipo, el tamaño, el MD5 y el SHA-256 de Drive,
la versión, la revisión y el SHA-256 local. Un archivo local nunca se
atribuye a otro `fileId`, y un archivo sin registro nunca se
sobrescribe.

**Integridad.**

1. Antes de descargar se exigen `capabilities.canDownload`, un tipo y
   una extensión admitidos, un tamaño declarado mayor que 0 y dentro
   del límite (25 MiB por defecto) y el `md5Checksum` de Drive. Sin
   checksum la imagen se rechaza: no se podría verificar.
2. La descarga se hace por bloques de 4 MiB a un temporal en la carpeta
   de destino, calculando MD5 y SHA-256. Si llegan más bytes de los
   declarados, se aborta.
3. Al terminar se comparan el tamaño, el MD5 y el SHA-256 de Drive
   (cuando lo informa). Pillow debe abrir y decodificar la imagen
   completa, su formato debe coincidir con el tipo declarado, y su
   protección contra bombas de descompresión se respeta: una imagen
   por encima de su límite se rechaza.
4. Una nueva consulta confirma que el MD5, el tamaño y la revisión no
   cambiaron en Drive durante la descarga.
5. Solo entonces se registra el SHA-256 como pendiente, se publica la
   imagen sin sobrescribir y se completa el registro.

Un archivo incompleto, alterado o corrupto nunca se publica: el
temporal se elimina y la imagen figura como fallida. Si el proceso se
interrumpe, al repetir el comando se omiten las imágenes ya
verificadas, se recuperan las publicadas cuyo registro quedó pendiente
(si su SHA-256 coincide) y se vuelve a descargar el resto. Los
temporales abandonados se eliminan después de 24 horas.

Cada bloque se reintenta ante errores 429, 5xx o de red, con un máximo
de 4 reintentos por imagen. Un error de autorización detiene el lote;
uno de permisos o de certificado SSL no se reintenta.

No se admiten descargas concurrentes sobre la misma carpeta local.

### Procesamiento de las descargas (GD-03A)

```
python -m image_processor.drive_cli process-local --folder-id ID --input downloads/prueba --output outputs/prueba --limit 3
python -m image_processor.drive_cli process-local --folder-id ID --input downloads/prueba --output outputs/prueba --background transparent --width 800 --height 600
python -m image_processor.drive_cli process-local --folder-id ID --input downloads/prueba --output outputs/prueba --dry-run
```

Procesa las imágenes ya descargadas con el mismo motor, la misma
política de nombres y el mismo manifiesto que el lote local. **No se
conecta a Drive ni lee la autorización**: trabaja solo con archivos
locales, nunca modifica las descargas ni sus registros y no sube nada.
Por defecto: fondo blanco, 400 x 400 px, PNG, modelo
`isnet-general-use`. El modelo se inicializa una sola vez por lote, y
solo si hay algo que procesar. `--dry-run` muestra qué se haría sin
inicializar el modelo ni escribir nada.

**Procedencia.** Solo se procesa una imagen con un registro completo de
GD-02 (`.drive/<nombre>.json`) que pertenece a la carpeta indicada en
`--folder-id`, nombra ese mismo archivo (su nombre local se deriva del
`fileId` registrado) y coincide en tamaño, MD5 y SHA-256 con el archivo.
Se rechazan las imágenes sin registro, con un registro dañado o
pendiente, y las alteradas; los enlaces simbólicos también. Las
descargas de otras carpetas se excluyen. Justo antes de procesar, los
bytes se vuelven a comparar con el SHA-256 registrado, y se procesan
esos mismos bytes.

**Identidad.** El original es `("drive", fileId)`, con el `fileId`
exacto: es un identificador opaco y nunca se normaliza (`1AbC` y `1abc`
son archivos distintos). Los originales locales se siguen comparando
sin distinguir mayúsculas. Si hay varias revisiones descargadas de un
mismo `fileId`, se procesa la vigente (mayor versión de Drive); los
resultados de las anteriores se conservan.

**Nombres.** `TRANSFORMADA_<nombre local>[variante].png`, por ejemplo
`TRANSFORMADA_producto--1~ab~c-_9.png`. El nombre local ya incluye el
`fileId` codificado, así que dos archivos remotos con el mismo nombre
nunca comparten resultado. Se aplican todas las reglas anteriores: las
variantes, las revisiones `__rev-` cuando el nombre pertenece a otro
original (por ejemplo, a un resultado del lote local), la publicación
sin sobrescribir y la recuperación tras una interrupción.

**Manifiesto.** Cada resultado de Drive registra el `fileId`, el nombre
remoto, el SHA-256 local y, en `source.drive`, la carpeta de origen, el
MD5 de Drive, la versión, la revisión, la fecha de modificación y el
nombre de la copia local, además del modelo, las opciones, la versión
del procesamiento y el SHA-256 del PNG. Un destino guarda resultados de
una sola carpeta de Drive. La subida (GD-03B) usará esta información
para saber qué PNG corresponde a cada fotografía y si está verificado.

Códigos de salida: 0 lote completo; 2 argumentos o carpetas inválidos
(incluida una carpeta dentro de la de secretos); 6 lote parcial
(imágenes rechazadas o fallidas, o resultados sin verificar o con el
registro pendiente); 7 no se pudo inicializar el modelo; 8 manifiesto
dañado o destino con resultados de otra carpeta de Drive.

### Subida de resultados (GD-03B)

```
python -m image_processor.drive_cli auth-write
python -m image_processor.drive_cli upload --folder-id ID --input outputs/gd03a-prueba --limit 1 --dry-run
python -m image_processor.drive_cli upload --folder-id ID --input outputs/gd03a-prueba --limit 1
python -m image_processor.drive_cli upload --folder-id ID --input outputs/gd03a-prueba --limit 3 --target-folder-id OTRO_ID
python -m image_processor.drive_cli logout --write
```

**Autorizaciones.** `auth-write` pide exactamente
`drive.file` y la guarda en `token_write.json`; nunca lee, modifica ni
reemplaza `token.json`. Un token de escritura con permisos más amplios
(por ejemplo, `drive`) o distintos se rechaza: el permiso `drive` nunca
se usa como alternativa. `upload` usa las dos autorizaciones, cada una
para lo mínimo:

- solo lectura: la carpeta de destino, el original de cada resultado y
  los nombres que ya existen en el destino (también los ajenos);
- `drive.file`: comprobar el acceso al destino, preasignar IDs, crear
  los PNG y consultar los PNG propios.

**Límite de `drive.file`.** Solo da acceso a los archivos que crea la
aplicación y a los que el usuario le comparte, por ejemplo con Google
Picker (todavía no integrado). Para crear un archivo dentro de una
carpeta, la carpeta debe ser accesible para la aplicación. Una carpeta
existente que la aplicación no creó, como la de prueba, normalmente
**no** lo es. `upload` lo comprueba antes de subir nada; si no hay
acceso, se detiene con el código 4 y lo explica. Que una carpeta sea
accesible (o elegida con Picker) no da acceso a los archivos que ya
contiene.

**Qué se sube.** Solo los PNG registrados como completos en el
manifiesto de GD-03A, de originales de la carpeta `--folder-id` y con
la configuración indicada (`--model`, `--background`, `--width`,
`--height`), cuyo SHA-256 coincide con el registrado y que Pillow
reconoce como el PNG esperado. El original debe seguir en esa carpeta
con el mismo MD5. Se rechazan los PNG sin registro, alterados,
enlaces simbólicos o de otra carpeta. Se suben los bytes verificados,
con una subida reanudable (`MediaIoBaseUpload`, la base de
`MediaFileUpload`, sobre esos mismos bytes en memoria). El destino es
la carpeta de los originales, u otra indicada con `--target-folder-id`.

**Duplicados.** Drive admite nombres repetidos, así que el nombre no
identifica. Cada subida tiene una identidad: el `fileId` y el SHA-256
del original, el SHA-256 del PNG, el modelo, las opciones, la versión
del procesamiento y la carpeta de destino. Se guarda en las
`appProperties` del PNG y en un registro local,
`<resultados>/.uploads/<carpeta de destino>/<png>.json`.

1. Si hay registro, se verifica el PNG remoto registrado: si coincide,
   no se vuelve a subir.
2. Si no hay registro, se busca en el destino un PNG con la misma
   identidad (todas las páginas).
3. Si no existe, se preasigna un ID con `files.generateIds`, se
   registra como pendiente y recién entonces se crea el archivo con
   `files.create` y ese ID. Los reintentos (429, 5xx, red) usan el
   mismo ID: si un intento anterior llegó a Drive, este responde 409 y
   no se duplica.
4. Si la respuesta se pierde y no se puede confirmar, la subida queda
   **incierta** (código 6). La siguiente ejecución consulta el ID
   reservado: confirma el archivo o lo crea con ese mismo ID.

Un PNG registrado que ya no existe en Drive (o está en la papelera) no
se vuelve a subir automáticamente. Para subirlo de nuevo, borre su
registro en `.uploads/`.

**Nombres.** `TRANSFORMADA_<nombre del original>[variante].png`. Si ese
nombre ya existe en el destino (un archivo ajeno u otro resultado), se
usa el nombre de revisión `__rev-<12 hex>`; si también existe, el PNG
se rechaza. Ningún archivo se sobrescribe.

**Verificación.** Tras crear cada PNG se consulta de nuevo: ID,
nombre, carpeta, `mimeType=image/png`, tamaño, MD5 (y SHA-256 si Drive
lo informa) e identidad. Un PNG que no coincide queda registrado como
no coincidente y nunca se da por completado ni se vuelve a crear.

**Vista previa y límite.** `--dry-run` no escribe nada en Drive ni en
disco; funciona también sin autorización de escritura. `--limit` es
obligatorio. El resumen muestra las subidas, las verificadas
previamente, las rechazadas, las fallidas y las pendientes (inciertas).

Códigos de salida: 0 completo; 2 argumentos inválidos; 3 autorización
de lectura o de escritura ausente, expirada o revocada; 4 sin permiso,
destino inexistente o sin acceso para `drive.file`, o almacenamiento
lleno; 5 red, cuota o certificado SSL; 6 lote parcial (rechazadas,
fallidas o inciertas); 8 manifiesto de resultados dañado. Un error de
autorización, de permisos o de certificado detiene el lote; tres fallos
de red seguidos, también.

No se admiten subidas concurrentes sobre la misma carpeta de resultados.

### Permisos y riesgos

- `drive.readonly` permite leer **todos** los archivos de la cuenta.
  Google lo clasifica como scope **restringido**: sirve para esta prueba
  local con una cuenta agregada como usuario de prueba, pero una
  aplicación pública que lo use puede requerir la verificación de
  Google y una evaluación de seguridad.
- En modo de prueba, Google puede hacer que el token de actualización
  expire a los pocos días. En ese caso basta con ejecutar `auth` de
  nuevo.
- `drive.file` (escritura, GD-03B) es un scope no sensible: la
  aplicación solo accede a los archivos que crea o que el usuario le
  comparte. Escribir en una carpeta existente requiere que la carpeta
  sea accesible para la aplicación (Google Picker, pendiente). Elegir
  una carpeta no garantiza acceso a los archivos que ya contiene.
- No se usan cuentas de servicio: la autorización es siempre personal.

## Estado

Prototipo en desarrollo.

## Tecnologías iniciales

- Python
- Pillow
- pytest

## Próximas integraciones

- Google Drive API: descarga, procesamiento y subida de resultados
- FastAPI
- Next.js