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

| Situación | Ejemplo de resultado |
|---|---|
| Original único, variante predeterminada | `TRANSFORMADA_producto.png` |
| Original único, otra variante | `TRANSFORMADA_producto__800x600_transparent.png` |
| Varios originales con el mismo nombre base | `TRANSFORMADA_producto__src-jpg.png` |
| Varios originales, otra variante | `TRANSFORMADA_producto__src-jpg__800x600_transparent.png` |

- **Nombre histórico:** un original que no comparte nombre base con
  otro conserva `TRANSFORMADA_<nombre>.png`, así que los resultados
  previos se siguen reconociendo.
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
- **Resultados existentes:** si el destino de una imagen ya existe,
  incluso con otras mayúsculas, se omite y nunca se sobrescribe.
- **Resultados históricos ambiguos:** si aparece un segundo original
  con el mismo nombre base, el histórico `TRANSFORMADA_producto.png`
  no se atribuye a ninguno. Se conserva intacto, cada original
  genera su resultado `__src-` y el lote muestra una advertencia.
  Lo mismo ocurre si después queda un solo original, pero el destino
  todavía tiene resultados `__src-` de ese nombre base.
- **Colisiones sin solución segura:** si dos originales reclaman el
  mismo nombre (por ejemplo, `producto.jpg` junto a otro original
  llamado `producto__src-jpg.png`), ninguno de los dos se procesa y
  cada uno se informa como fallido, con la indicación de renombrar.
  Las demás imágenes del lote se procesan normalmente.

Limitaciones hasta que exista un manifiesto (H-05):

- La identidad de un resultado depende solo de su nombre. No se
  detecta si un original cambió de contenido.
- El nombre no incluye el modelo: reprocesar con otro modelo omite
  los resultados existentes de la misma variante.
- Agregar un segundo original con el mismo nombre base hace que el
  primero se vuelva a procesar con el sufijo `__src-`.

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