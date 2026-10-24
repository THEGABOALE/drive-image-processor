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

## Estado

Prototipo en desarrollo.

## Tecnologías iniciales

- Python
- Pillow
- pytest

## Próximas integraciones

- rembg
- Google Drive API
- FastAPI
- Next.js