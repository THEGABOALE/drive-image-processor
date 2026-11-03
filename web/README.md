# Aplicación web local

Interfaz local para Windows sobre el motor de `image_processor`, que se usa
como biblioteca y no cambia. Se abre en el navegador y escucha solo en
`127.0.0.1`.

Permite conectar una cuenta de Google, elegir las carpetas con Google Picker,
revisar una vista previa, procesar las imágenes y ver el progreso y el resumen.
Se distribuye como una carpeta comprimida con `DriveImageProcessor.exe`.

## Arquitectura

```
Navegador (React + Vite)              Backend local (FastAPI, 127.0.0.1:8000)
- Cliente OAuth web                   - Cliente OAuth de escritorio
- Token temporal drive.file + openid  - Tokens propios, cifrados con DPAPI
- Google Picker                       - Motor: auth, client, picker, queries
        |                                       ^
        +---- solo los ID de las carpetas ------+
```

- **Desarrollo:** Vite en `localhost:5173` y FastAPI en `localhost:8000`. Vite
  reenvía `/api`, así que para el navegador es el mismo origen y no hay CORS.
- **Empaquetado futuro:** FastAPI sirve `web/frontend/dist` en `localhost:8000`.
- **Usar `localhost`, no `127.0.0.1`,** en el navegador: es el origen autorizado
  en Google y el único que acepta el backend.

## Iniciar

```bash
python -m pip install -r web/backend/requirements.txt
```

```bash
npm --prefix web/frontend install
```

Desarrollo, en dos terminales desde la raíz del repositorio:

```bash
npm --prefix web/frontend run dev
```

```bash
python -m web.backend.app
```

Producción (`DIP_ENV=production`): compilar el frontend y ejecutar lo mismo sin
Vite. El comando abre el navegador; `--no-browser` lo evita.

```bash
npm --prefix web/frontend run build
```

Pruebas:

```bash
python -m pytest
```

```bash
npm --prefix web/frontend test
```

## Variables de configuración

Se leen de `web/backend/.env.dev.local` o
`web/backend/.env.production.local` (ignorados por Git). Las variables reales
tienen prioridad. Plantilla: `web/backend/.env.example`.

| Variable | Contenido |
|---|---|
| `DIP_ENV` | `development` (por defecto) o `production`. Se define en el entorno, no en el archivo. |
| `GOOGLE_CLOUD_PROJECT_ID` | ID del proyecto de este entorno. |
| `GOOGLE_CLOUD_PROJECT_NUMBER` | Número del proyecto. También es el App ID de Picker. |
| `GOOGLE_DESKTOP_CLIENT_FILE` | Ruta al JSON del cliente OAuth de escritorio. Solo se lee. |
| `GOOGLE_PICKER_CLIENT_ID` | ID del cliente OAuth web. Público. |
| `GOOGLE_PICKER_API_KEY` | Clave de API de Picker, restringida. Pública. |

Al iniciar se comprueba que:

- el JSON sea realmente de un cliente de escritorio (`installed`) y del proyecto
  configurado;
- los dos clientes tengan como prefijo el número del proyecto y sean distintos;
- el proyecto no sea del otro entorno (`-prod` en desarrollo, `-dev` en
  producción).

Los datos se guardan en `%LOCALAPPDATA%\DriveImageProcessor\<entorno>`. Los tokens
están cifrados con DPAPI, ligados al usuario de Windows y al entorno. Sin DPAPI
la aplicación no arranca: no hay alternativa en texto plano.

## Permisos e identidad

| Autorización | Cliente | Permisos | Dónde queda |
|---|---|---|---|
| Lectura | Escritorio | `drive.readonly`, `openid` | Backend, cifrada |
| Escritura | Escritorio | `drive.file`, `openid` | Backend, cifrada |
| Picker | Web | `drive.file`, `openid` | Memoria del navegador |

**Por qué `openid`.** Es el único permiso agregado. Permite obtener el `sub`, el
identificador estable de la cuenta, y ningún otro dato. No se piden `email` ni
`profile`: el correo que se muestra viene de la API de Drive y nunca decide nada.

**Comprobación de la misma cuenta:**

1. El backend verifica la firma, el emisor, la audiencia y la vigencia del ID
   token de cada autorización, y exige el mismo `sub`. Si no coincide, no guarda
   nada.
2. El navegador consulta a Google el `sub` de su propio token y lo compara con el
   del backend antes de abrir Picker. Si no coincide o no se puede comprobar,
   Picker no se abre.
3. El backend comprueba que su propio `drive.file` vea la carpeta elegida, tanto
   de origen como de destino. Google concede `drive.file` por cuenta y proyecto,
   así que una carpeta elegida con otra cuenta no es visible.

**Límite conocido.** El paso 2 ocurre en el navegador porque el token no se envía
al backend y `initTokenClient` no entrega un ID token firmado. El backend no
puede verificar por sí mismo qué cuenta usó Picker. Lo que sí garantiza, sin
depender del frontend, es el paso 3: la cuenta conectada tiene autorizada esa
carpeta. Si otra cuenta eligiera una carpeta que la conectada ya había
autorizado, se aceptaría; es inofensivo, porque el acceso ya existía.

**Desconectar** borra los tokens locales y no revoca nada en Google. La revocación
quita todos los permisos que esa cuenta dio al proyecto, también los de la CLI.
Para revocar: <https://myaccount.google.com/permissions>.

La CLI y la aplicación comparten el cliente de escritorio en desarrollo, pero no
los tokens: la aplicación nunca lee ni escribe `.secrets/token*.json`.

## Vista previa y procesamiento

1. **Vista previa:** el motor arma el plan (`sync` con `dry_run=True`), sin escribir
   en Drive ni en disco y sin cargar el modelo. Lo que depende de una descarga o de
   un procesamiento se sabe recién al ejecutar.
2. **Procesar:** solo se puede ejecutar la última vista previa, terminada, sin usar
   y de hace menos de una hora, y de ella salen las carpetas: el frontend no las
   envía. El permiso se comprueba de nuevo antes de empezar.
3. **Progreso y resumen:** el trabajo corre en el backend. Se puede cerrar la pestaña
   y volver mientras la aplicación siga abierta. El último trabajo de cada cuenta se
   guarda en disco.

Hay un solo trabajo a la vez. Mientras uno está activo no se puede desconectar la
cuenta ni iniciar otro, y **no se puede cancelar**, porque el motor no lo permite.
Si la aplicación se cierra, el trabajo figura como interrumpido: al ejecutar de
nuevo se retoma sin duplicar.

Usa los valores del motor: 400×400, fondo blanco y todas las imágenes de la carpeta.
Los originales nunca se modifican ni se borran.

**Limitación.** El motor rehace el plan al ejecutar: lo que se agregue a la carpeta
después de la vista previa también se procesa. Por eso la vista previa vence.

**Datos locales**, en `%LOCALAPPDATA%\DriveImageProcessor\<entorno>\accounts\<cuenta>`:

- `folders\<origen>\downloads` y `outputs`: originales, resultados y los registros
  del motor. **No hay que borrarlos:** son los que evitan los duplicados en Drive. El
  espacio que ocupan crece con cada carpeta.
- `last_job.json`: el último trabajo.

La primera ejecución descarga el modelo de IA a la caché de rembg y puede tardar
varios minutos.

## Ejecutable para Windows

```bash
python -m pip install "pyinstaller>=6.10,<7"
```

```bash
python web/packaging/build.py --config web/packaging/config
```

`web/packaging/config` (ignorada por Git) tiene dos archivos:

- `app.env`: `DIP_ENV` y las variables de la tabla de arriba, con
  `GOOGLE_DESKTOP_CLIENT_FILE=desktop_client.json`;
- `desktop_client.json`: el JSON del cliente de escritorio de ese entorno.

El script valida la configuración con las reglas de la aplicación, compila el
frontend, toma el modelo de la caché de rembg (lo descarga si falta), arma el
programa con PyInstaller (una carpeta, con consola), le agrega `config/`, el
modelo y `LEEME.txt`, prueba el ejecutable con `--selftest` y deja
`web/packaging/DriveImageProcessor-<entorno>.zip`.

**El ejecutable:**

- muestra una consola: cerrarla cierra la aplicación;
- si ya está abierta, solo abre el navegador;
- ante un error, lo muestra y espera Enter para que no se cierre sin explicación;
- usa la configuración de su carpeta `config/`, no las variables del equipo, y el
  modelo de su carpeta `rembg/`;
- `DriveImageProcessor.exe --selftest` procesa una imagen de prueba, sin Drive.

**Credenciales en el paquete.** Lleva, en texto, el cliente de escritorio y la
clave de API de Picker. Google no considera confidencial el secreto de un
cliente de aplicación instalada, y la clave es pública y está restringida. Nunca
lleva tokens. Con la configuración de desarrollo, solo funciona para los
usuarios de prueba.

**Primer arranque.** Al importarse, rembg carga pymatting, que usa numba: la
primera vez compila, lo que tarda unos 50 segundos. El script deja esa caché en
el paquete al probarlo; en un equipo con otro procesador numba puede volver a
compilar una vez.

**Tamaño.** Unos 610 MB descomprimido, de los que 180 MB son el modelo.

## Configuración manual en Google Cloud

No se pega ninguna credencial en el chat ni en el repositorio.

### Desarrollo: `drive-image-processor-dev`

1. **APIs habilitadas:** Google Drive API y Google Picker API.
2. **Pantalla de consentimiento:** External, en Testing, con su cuenta como único
   usuario de prueba.
3. **Permisos declarados:** `drive.readonly`, `drive.file` y `openid`.
4. **Cliente de escritorio:** reutilizar el de la CLI, sin modificarlo.
5. **Cliente web nuevo:** tipo *Aplicación web*, con orígenes de JavaScript
   autorizados `http://localhost:5173` y `http://localhost:8000`. No necesita
   URI de redirección y su secreto no se usa.
6. **Clave de API nueva:**
   - restricción de sitios web: `http://localhost:5173/*`,
     `http://localhost:8000/*` y `https://docs.google.com/*`;
   - restricción de API: solo Google Picker API.
7. **Archivo:** crear `web/backend/.env.dev.local` desde
   `web/backend/.env.example`. `GOOGLE_DESKTOP_CLIENT_FILE` puede apuntar al
   `.secrets/credentials.json` existente.

En Testing, Google invalida los tokens de actualización a los 7 días: la
aplicación pedirá volver a conectar. Los tokens de la CLI caducan igual, cada uno
según cuándo se autorizó: se renuevan con `auth` y `auth-write`.

### Producción: `drive-image-processor-prod`

1. **Proyecto independiente**, con Google Drive API y Google Picker API.
2. **Pantalla de consentimiento:** External, publicada como *In Production*, sin
   usuarios de prueba y sin limitar correos ni dominios.
3. **Permisos declarados:** los mismos tres.
4. **Clientes propios:** uno de escritorio y uno web, del mismo proyecto. El web
   solo con el origen `http://localhost:8000`.
5. **Clave de API propia:** sitios `http://localhost:8000/*` y
   `https://docs.google.com/*`; API: solo Google Picker API.
6. **Archivo:** `web/backend/.env.production.local`, y `DIP_ENV=production` al
   iniciar.

## Cualquier cuenta de Google

El código no tiene una lista de correos ni de dominios: el `sub` solo se compara
entre las autorizaciones de la misma conexión. Quién puede autorizar lo decide la
configuración del proyecto en Google: en *In Production* no hay lista de usuarios
de prueba.

Las cuentas de Google Workspace pueden conectarse si la política del administrador
permite aplicaciones externas. Un administrador puede bloquearlas, y la aplicación
no intenta evitarlo.

## Publicación y verificación

**Publicar como *In Production* no es estar verificada.** Esta aplicación **no está
verificada** y `drive.readonly` es un permiso restringido. Hasta completar la
verificación de Google:

- los usuarios ven la pantalla de aplicación no verificada;
- hay un límite de **100 usuarios nuevos en total**, y Google puede ajustarlo;
- las organizaciones de Workspace pueden bloquearla.

`drive.file` no es sensible; lo que obliga a verificar es `drive.readonly`.

**Requisitos de la verificación de permisos restringidos:**

- verificación de la marca, y que el tipo de aplicación esté permitido para el
  producto;
- revisión más extensa, que puede tardar varias semanas;
- evaluación de seguridad (CASA) con un evaluador independiente aprobado por
  Google, renovada al menos cada 12 meses, si la aplicación puede acceder a los
  datos desde o a través de un servidor de terceros.

La documentación no trata de forma explícita el caso de una aplicación solo local.
Como los datos no pasan por un servidor propio, parecería no requerir la
evaluación, pero **es una inferencia que hay que confirmar con Google**. Este
trámite no bloquea el desarrollo.

Fuentes:

- <https://support.google.com/cloud/answer/7454865>
- <https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification>
- <https://developers.google.com/identity/protocols/oauth2/native-app>

## Verificación crítica de autorización (manual)

Hay que hacerla antes de avanzar a la siguiente fase, porque depende de Google
real. No crea ni modifica nada en Drive.

1. Conectar la cuenta: dos autorizaciones. Debe mostrarse el correo.
2. Elegir el origen y el destino con Picker. Debe abrirse sin error de clave.
   Usar carpetas nunca elegidas con la CLI: el permiso `drive.file` es por cuenta
   y proyecto, así que una carpeta ya autorizada no demuestra nada.
3. Pulsar **Verificar carpetas**: debe confirmar el acceso.
4. Repetir con **misma carpeta**.
5. Con una segunda cuenta agregada como usuario de prueba, abrir la aplicación en
   una ventana de incógnito, pulsar **Elegir carpeta de origen** y entrar con esa
   cuenta: debe mostrar que es otra cuenta y no abrir Picker. Sin ese alta, Google
   la bloquea antes (`403 access_denied`) y la comprobación de la aplicación no
   llega a ejecutarse.
6. **Desconectar** y comprobar que la CLI sigue funcionando.

Si en el paso 3 aparece *carpeta no autorizada*, la autorización no llega del
cliente web al de escritorio. En ese caso hay que detenerse: no se amplían permisos
ni se usa `drive`.
