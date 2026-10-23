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

- Variante predeterminada (400 x 400, fondo blanco):
  `TRANSFORMADA_producto.png`. Es el nombre histórico, así que los
  resultados previos se siguen reconociendo.
- Cualquier otra variante agrega sus dimensiones y su fondo:
  `TRANSFORMADA_producto__800x600_transparent.png`.
- Si el resultado de una variante ya existe, se omite y nunca se
  sobrescribe. Cada variante es un archivo independiente.
- El nombre no incluye el modelo ni la extensión del original.

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