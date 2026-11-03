// Google Picker en el navegador, con Google Identity Services.
//
// El token de esta autorización es temporal, pertenece al cliente OAuth web y
// solo vive en memoria: no se guarda ni se envía al backend, que recibe
// únicamente el ID de la carpeta. Antes de abrir Picker se compara su cuenta
// con la del backend, consultando el sub directamente a Google. El backend
// vuelve a comprobarlo por su lado: su drive.file debe ver la carpeta elegida.

declare global {
  interface Window {
    // Las bibliotecas de Google no tienen tipos en este proyecto.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    google: any;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    gapi: any;
  }
}

export const DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file";
const OPENID_SCOPE = "openid";
export const FOLDER_MIME = "application/vnd.google-apps.folder";
export const USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo";

const GSI_SCRIPT = "https://accounts.google.com/gsi/client";
const GAPI_SCRIPT = "https://apis.google.com/js/api.js";

// Mismo formato que valida el motor (queries.validate_file_id).
const FILE_ID = /^[A-Za-z0-9_-]+$/;

// Un token a punto de vencer no se reutiliza.
const EXPIRY_MARGIN_MS = 60_000;

export type PickerConfig = {
  client_id: string;
  api_key: string;
  project_number: string;
};

export type PickedFolder = { id: string; name: string };

export type PickerErrorCode =
  | "load_failed"
  | "popup_blocked"
  | "authorization_cancelled"
  | "scope_mismatch"
  | "account_mismatch"
  | "identity_unverified"
  | "invalid_selection";

export class PickerError extends Error {
  constructor(
    readonly code: PickerErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "PickerError";
  }
}

function loadScript(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(src));
    document.head.appendChild(script);
  });
}

/** Carga las bibliotecas oficiales de Google. Picker las necesita listas al hacer clic. */
export async function loadGoogleApis(): Promise<void> {
  try {
    await Promise.all([loadScript(GSI_SCRIPT), loadScript(GAPI_SCRIPT)]);
    await new Promise<void>((resolve, reject) => {
      window.gapi.load("picker", { callback: resolve, onerror: reject });
    });
  } catch {
    throw new PickerError(
      "load_failed",
      "No se pudo cargar Google Picker. Revise la conexión a internet.",
    );
  }
}

export class GooglePicker {
  private token: { value: string; expiresAt: number } | null = null;

  constructor(
    private readonly config: PickerConfig,
    private readonly expectedSub: string,
  ) {}

  /**
   * Abre Picker para elegir una carpeta existente. Devuelve null si se
   * cancela. Debe llamarse desde un clic: la autorización abre una ventana.
   */
  async pick(title: string): Promise<PickedFolder | null> {
    const token = await this.accessToken();
    return this.openPicker(token, title);
  }

  forget(): void {
    this.token = null;
  }

  private async accessToken(): Promise<string> {
    if (!this.expectedSub) {
      throw new PickerError(
        "identity_unverified",
        "No se conoce la cuenta conectada: no se puede abrir Google Picker.",
      );
    }

    if (this.token && Date.now() < this.token.expiresAt - EXPIRY_MARGIN_MS) {
      return this.token.value;
    }

    this.token = null;
    const response = await this.requestToken();

    assertScopes(response.scope);
    await this.assertSameAccount(response.access_token);

    this.token = {
      value: response.access_token,
      expiresAt: Date.now() + Number(response.expires_in) * 1000,
    };
    return this.token.value;
  }

  private requestToken(): Promise<{
    access_token: string;
    expires_in: number | string;
    scope: string;
  }> {
    return new Promise((resolve, reject) => {
      const client = window.google.accounts.oauth2.initTokenClient({
        client_id: this.config.client_id,
        scope: `${DRIVE_FILE_SCOPE} ${OPENID_SCOPE}`,
        // Sin esto el token heredaría otros permisos del proyecto.
        include_granted_scopes: false,
        login_hint: this.expectedSub,
        prompt: "",
        callback: (response: {
          error?: string;
          access_token: string;
          expires_in: number | string;
          scope: string;
        }) => {
          if (response.error) {
            reject(
              new PickerError(
                "authorization_cancelled",
                "No se concedió el permiso para abrir Google Picker.",
              ),
            );
          } else {
            resolve(response);
          }
        },
        error_callback: (error: { type?: string }) => {
          reject(
            error.type === "popup_failed_to_open"
              ? new PickerError(
                  "popup_blocked",
                  "El navegador bloqueó la ventana de Google. Permita las ventanas emergentes de esta página.",
                )
              : new PickerError(
                  "authorization_cancelled",
                  "Se cerró la ventana de autorización de Google.",
                ),
          );
        },
      });

      client.requestAccessToken();
    });
  }

  // Un token de otra cuenta se descarta sin revocarlo: Google quitaría todos
  // los permisos del proyecto a esa cuenta.
  private async assertSameAccount(accessToken: string): Promise<void> {
    const unverified = new PickerError(
      "identity_unverified",
      "No se pudo comprobar con Google qué cuenta está usando el navegador. No se abrió Google Picker.",
    );
    let sub: unknown;

    try {
      const response = await fetch(USERINFO_URL, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) throw unverified;
      sub = (await response.json()).sub;
    } catch {
      throw unverified;
    }

    if (typeof sub !== "string" || !sub) throw unverified;

    if (sub !== this.expectedSub) {
      throw new PickerError(
        "account_mismatch",
        "Eligió otra cuenta de Google. Use la misma cuenta que conectó en esta aplicación.",
      );
    }
  }

  private openPicker(token: string, title: string): Promise<PickedFolder | null> {
    const { picker } = window.google;

    return new Promise((resolve, reject) => {
      const folderView = () =>
        new picker.DocsView(picker.ViewId.FOLDERS)
          .setSelectFolderEnabled(true)
          .setIncludeFolders(true)
          .setMimeTypes(FOLDER_MIME)
          .setMode(picker.DocsViewMode.LIST);

      // Dos pestañas: con setEnableDrives la vista muestra solo las unidades
      // compartidas, así que no puede ser la única ni la primera.
      new picker.PickerBuilder()
        .addView(folderView())
        .addView(folderView().setEnableDrives(true))
        .setOAuthToken(token)
        .setDeveloperKey(this.config.api_key)
        .setAppId(this.config.project_number)
        .setTitle(title)
        .setCallback((data: Record<string, unknown>) => {
          const action = data[picker.Response.ACTION];

          if (action === picker.Action.CANCEL) {
            resolve(null);
          } else if (action === picker.Action.PICKED) {
            try {
              resolve(selectedFolder(data[picker.Response.DOCUMENTS]));
            } catch (error) {
              reject(error);
            }
          }
        })
        .build()
        .setVisible(true);
    });
  }
}

function assertScopes(granted: string): void {
  const scopes = new Set((granted ?? "").split(" ").filter(Boolean));

  if (scopes.size !== 2 || !scopes.has(DRIVE_FILE_SCOPE) || !scopes.has(OPENID_SCOPE)) {
    throw new PickerError(
      "scope_mismatch",
      "Google no concedió exactamente los permisos necesarios para Google Picker.",
    );
  }
}

function selectedFolder(documents: unknown): PickedFolder {
  const { Document } = window.google.picker;
  const items = Array.isArray(documents) ? documents : [];
  const item = items[0];

  if (
    items.length !== 1 ||
    item[Document.MIME_TYPE] !== FOLDER_MIME ||
    !FILE_ID.test(String(item[Document.ID]))
  ) {
    throw new PickerError("invalid_selection", "Elija una sola carpeta.");
  }

  return { id: item[Document.ID], name: item[Document.NAME] };
}
