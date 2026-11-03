# Aplicación web local

Interfaz local para Windows sobre el motor de `image_processor`, que se usa
como biblioteca y no cambia. Se abre en el navegador y escucha solo en
`127.0.0.1`.

Fase 1: conectar y desconectar una cuenta de Google, elegir las carpetas con
Google Picker y verificarlas. Todavía no procesa imágenes.

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
