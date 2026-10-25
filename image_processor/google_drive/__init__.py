"""
Integración de solo lectura con Google Drive (GD-01).

Módulos:

- auth: OAuth 2.0 para aplicaciones de escritorio y token local.
- client: construcción del cliente y ejecución con reintentos.
- queries: conexión, carpetas e imágenes de una carpeta.
- errors: errores de autenticación, acceso y red.

Esta capa solo consulta metadatos: no descarga, sube, modifica
ni elimina archivos de Drive.
"""
