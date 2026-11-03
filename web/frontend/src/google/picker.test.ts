import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DRIVE_FILE_SCOPE,
  FOLDER_MIME,
  GooglePicker,
  PickerError,
  USERINFO_URL,
  loadGoogleApis,
} from "./picker";

const CONFIG = {
  client_id: "123-web.apps.googleusercontent.com",
  api_key: "clave-publica",
  project_number: "123",
};
const TOKEN = "token-temporal-del-navegador";
const GRANTED = `${DRIVE_FILE_SCOPE} openid`;

type TokenReply =
  | { kind: "token"; scope?: string; expires_in?: number }
  | { kind: "oauth_error"; error: string }
  | { kind: "popup"; type: string };

function fakeGoogle(reply: TokenReply = { kind: "token" }) {
  const state = {
    reply,
    tokenConfigs: [] as Record<string, unknown>[],
    requests: 0,
    builder: {} as Record<string, unknown>,
    views: [] as Record<string, unknown>[],
    callback: null as null | ((data: Record<string, unknown>) => void),
    visible: 0,
    revoked: 0,
  };

  class DocsView {
    options: Record<string, unknown>;
    constructor(viewId: string) {
      this.options = { viewId };
    }
    setSelectFolderEnabled(value: boolean) {
      this.options.selectFolder = value;
      return this;
    }
    setIncludeFolders(value: boolean) {
      this.options.includeFolders = value;
      return this;
    }
    setMimeTypes(value: string) {
      this.options.mimeTypes = value;
      return this;
    }
    setMode(value: string) {
      this.options.mode = value;
      return this;
    }
    setEnableDrives(value: boolean) {
      this.options.drives = value;
      return this;
    }
  }

  class PickerBuilder {
    addView(view: DocsView) {
      state.views.push(view.options);
      return this;
    }
    setOAuthToken(value: string) {
      state.builder.token = value;
      return this;
    }
    setDeveloperKey(value: string) {
      state.builder.key = value;
      return this;
    }
    setAppId(value: string) {
      state.builder.appId = value;
      return this;
    }
    setTitle(value: string) {
      state.builder.title = value;
      return this;
    }
    setCallback(value: (data: Record<string, unknown>) => void) {
      state.callback = value;
      return this;
    }
    enableFeature(value: string) {
      state.builder.feature = value;
      return this;
    }
    build() {
      return {
        setVisible: () => {
          state.visible += 1;
        },
      };
    }
  }

  (window as unknown as { google: unknown }).google = {
    accounts: {
      oauth2: {
        revoke: () => {
          state.revoked += 1;
        },
        initTokenClient: (config: Record<string, unknown>) => {
          state.tokenConfigs.push(config);
          return {
            requestAccessToken: () => {
              state.requests += 1;
              const answer = state.reply;
              if (answer.kind === "popup") {
                (config.error_callback as (e: unknown) => void)({ type: answer.type });
              } else if (answer.kind === "oauth_error") {
                (config.callback as (r: unknown) => void)({ error: answer.error });
              } else {
                (config.callback as (r: unknown) => void)({
                  access_token: TOKEN,
                  expires_in: answer.expires_in ?? 3600,
                  scope: answer.scope ?? GRANTED,
                });
              }
            },
          };
        },
      },
    },
    picker: {
      DocsView,
      PickerBuilder,
      ViewId: { FOLDERS: "folders" },
      DocsViewMode: { LIST: "list" },
      Action: { PICKED: "picked", CANCEL: "cancel" },
      Response: { ACTION: "action", DOCUMENTS: "docs" },
      Document: { ID: "id", NAME: "name", MIME_TYPE: "mimeType" },
    },
  };

  return state;
}

function mockUserinfo(response: { ok?: boolean; sub?: string } | "network") {
  const fetchMock = vi.fn(async () => {
    if (response === "network") throw new TypeError("sin red");
    return {
      ok: response.ok ?? true,
      json: async () => ({ sub: response.sub }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const folderDoc = { id: "carpeta1", name: "Fotos", mimeType: FOLDER_MIME };

async function waitForPicker(state: ReturnType<typeof fakeGoogle>) {
  await vi.waitFor(() => expect(state.visible).toBeGreaterThan(0));
}

async function pickWith(
  state: ReturnType<typeof fakeGoogle>,
  data: Record<string, unknown>,
) {
  const picker = new GooglePicker(CONFIG, "sub-a");
  const result = picker.pick("Elegir origen");
  await waitForPicker(state);
  state.callback!(data);
  return result;
}

describe("autorización del navegador", () => {
  beforeEach(() => {
    mockUserinfo({ sub: "sub-a" });
  });

  it("pide solo drive.file y openid, para la cuenta conectada", async () => {
    const state = fakeGoogle();
    const picker = new GooglePicker(CONFIG, "sub-a");
    const result = picker.pick("Elegir origen");
    await waitForPicker(state);
    state.callback!({ action: "cancel" });
    await result;

    expect(state.tokenConfigs).toHaveLength(1);
    const config = state.tokenConfigs[0];
    expect(config.client_id).toBe(CONFIG.client_id);
    expect(String(config.scope).split(" ").sort()).toEqual(
      [DRIVE_FILE_SCOPE, "openid"].sort(),
    );
    expect(config.login_hint).toBe("sub-a");
    // No hereda otros permisos del proyecto, como drive.readonly.
    expect(config.include_granted_scopes).toBe(false);
  });

  it("detiene todo si se cierra la ventana de autorización", async () => {
    const state = fakeGoogle({ kind: "popup", type: "popup_closed" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "authorization_cancelled",
    });
    expect(state.visible).toBe(0);
  });

  it("informa si el navegador bloqueó la ventana", async () => {
    const state = fakeGoogle({ kind: "popup", type: "popup_failed_to_open" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "popup_blocked",
    });
    expect(state.visible).toBe(0);
  });

  it("detiene todo si el usuario rechaza el permiso", async () => {
    const state = fakeGoogle({ kind: "oauth_error", error: "access_denied" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "authorization_cancelled",
    });
    expect(state.visible).toBe(0);
  });

  it.each([
    ["sin drive.file", "openid"],
    ["sin openid", DRIVE_FILE_SCOPE],
    ["con drive.readonly", `${GRANTED} https://www.googleapis.com/auth/drive.readonly`],
    ["con drive completo", `${GRANTED} https://www.googleapis.com/auth/drive`],
    ["con email", `${GRANTED} email`],
  ])("rechaza un token %s", async (_name, scope) => {
    const state = fakeGoogle({ kind: "token", scope });
    const fetchMock = mockUserinfo({ sub: "sub-a" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "scope_mismatch",
    });
    expect(state.visible).toBe(0);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("identidad", () => {
  it("consulta el sub directamente a Google con el token del navegador", async () => {
    const state = fakeGoogle();
    const fetchMock = mockUserinfo({ sub: "sub-a" });

    const result = pickWith(state, { action: "cancel" });
    await result;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(USERINFO_URL);
    expect(new URL(url).hostname).toBe("www.googleapis.com");
    expect((init.headers as Record<string, string>).Authorization).toBe(`Bearer ${TOKEN}`);
  });

  it("no abre Picker si la cuenta del navegador es otra", async () => {
    const state = fakeGoogle();
    mockUserinfo({ sub: "sub-otra" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "account_mismatch",
    });
    expect(state.visible).toBe(0);
  });

  it("no revoca el token de otra cuenta: quitaría sus permisos del proyecto", async () => {
    const state = fakeGoogle();
    mockUserinfo({ sub: "sub-otra" });

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toBeInstanceOf(
      PickerError,
    );
    expect(state.revoked).toBe(0);
  });

  it("descarta el token de otra cuenta: el siguiente intento vuelve a pedirlo", async () => {
    const state = fakeGoogle();
    mockUserinfo({ sub: "sub-otra" });
    const picker = new GooglePicker(CONFIG, "sub-a");

    await expect(picker.pick("t")).rejects.toBeInstanceOf(PickerError);
    mockUserinfo({ sub: "sub-a" });
    const result = picker.pick("t");
    await waitForPicker(state);
    state.callback!({ action: "cancel" });
    await result;

    expect(state.requests).toBe(2);
  });

  it.each([
    ["una respuesta de error", { ok: false, sub: "sub-a" }],
    ["una respuesta sin sub", {}],
    ["un fallo de red", "network" as const],
  ])("identidad ambigua con %s: no abre Picker", async (_name, response) => {
    const state = fakeGoogle();
    mockUserinfo(response);

    await expect(new GooglePicker(CONFIG, "sub-a").pick("t")).rejects.toMatchObject({
      code: "identity_unverified",
    });
    expect(state.visible).toBe(0);
  });

  it("sin una cuenta esperada no pide ningún token", async () => {
    const state = fakeGoogle();

    await expect(new GooglePicker(CONFIG, "").pick("t")).rejects.toMatchObject({
      code: "identity_unverified",
    });
    expect(state.requests).toBe(0);
  });
});

describe("Google Picker", () => {
  beforeEach(() => {
    mockUserinfo({ sub: "sub-a" });
  });

  it("usa el token, la clave y el número de proyecto", async () => {
    const state = fakeGoogle();
    await pickWith(state, { action: "cancel" });

    expect(state.builder.token).toBe(TOKEN);
    expect(state.builder.key).toBe(CONFIG.api_key);
    expect(state.builder.appId).toBe(CONFIG.project_number);
    expect(state.builder.title).toBe("Elegir origen");
  });

  it("muestra solo carpetas existentes y una sola selección", async () => {
    const state = fakeGoogle();
    await pickWith(state, { action: "cancel" });

    expect(state.views.length).toBeGreaterThan(0);
    for (const view of state.views) {
      expect(view).toMatchObject({
        viewId: "folders",
        selectFolder: true,
        includeFolders: true,
        mimeTypes: FOLDER_MIME,
        mode: "list",
      });
    }
    expect(state.builder.feature).toBeUndefined();
  });

  it("abre en las carpetas normales y deja las unidades compartidas en otra pestaña", async () => {
    const state = fakeGoogle();
    await pickWith(state, { action: "cancel" });

    // Con setEnableDrives, la vista muestra solo las unidades compartidas.
    expect(state.views).toHaveLength(2);
    expect(state.views[0].drives).toBeUndefined();
    expect(state.views[1].drives).toBe(true);
  });

  it("devuelve la carpeta elegida", async () => {
    const state = fakeGoogle();

    await expect(
      pickWith(state, { action: "picked", docs: [folderDoc] }),
    ).resolves.toEqual({ id: "carpeta1", name: "Fotos" });
  });

  it("devuelve null si se cancela", async () => {
    const state = fakeGoogle();

    await expect(pickWith(state, { action: "cancel" })).resolves.toBeNull();
  });

  it("ignora los eventos intermedios", async () => {
    const state = fakeGoogle();
    const picker = new GooglePicker(CONFIG, "sub-a");
    const result = picker.pick("t");
    await waitForPicker(state);

    state.callback!({ action: "loaded" });
    state.callback!({ action: "picked", docs: [folderDoc] });

    await expect(result).resolves.toEqual({ id: "carpeta1", name: "Fotos" });
  });

  it.each([
    ["varios elementos", [folderDoc, { ...folderDoc, id: "carpeta2" }]],
    ["ninguno", []],
    ["un archivo", [{ ...folderDoc, mimeType: "image/jpeg" }]],
    ["un ID inválido", [{ ...folderDoc, id: "a'b" }]],
  ])("rechaza %s", async (_name, docs) => {
    const state = fakeGoogle();

    await expect(pickWith(state, { action: "picked", docs })).rejects.toMatchObject({
      code: "invalid_selection",
    });
  });
});

describe("el token solo vive en memoria", () => {
  beforeEach(() => {
    mockUserinfo({ sub: "sub-a" });
  });

  afterEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("no usa LocalStorage ni SessionStorage", async () => {
    const state = fakeGoogle();
    await pickWith(state, { action: "picked", docs: [folderDoc] });

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("reutiliza el token entre selecciones, sin volver a preguntar", async () => {
    const state = fakeGoogle();
    const picker = new GooglePicker(CONFIG, "sub-a");

    for (let i = 1; i <= 2; i += 1) {
      const result = picker.pick("t");
      await vi.waitFor(() => expect(state.visible).toBe(i));
      state.callback!({ action: "cancel" });
      await result;
    }

    expect(state.requests).toBe(1);
  });

  it("pide otro token y revisa la identidad cuando expira", async () => {
    const state = fakeGoogle({ kind: "token", expires_in: 1 });
    const fetchMock = mockUserinfo({ sub: "sub-a" });
    const picker = new GooglePicker(CONFIG, "sub-a");

    for (let i = 1; i <= 2; i += 1) {
      const result = picker.pick("t");
      await vi.waitFor(() => expect(state.visible).toBe(i));
      state.callback!({ action: "cancel" });
      await result;
    }

    expect(state.requests).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("forget olvida el token", async () => {
    const state = fakeGoogle();
    const picker = new GooglePicker(CONFIG, "sub-a");
    let result = picker.pick("t");
    await vi.waitFor(() => expect(state.visible).toBe(1));
    state.callback!({ action: "cancel" });
    await result;

    picker.forget();
    result = picker.pick("t");
    await vi.waitFor(() => expect(state.visible).toBe(2));
    state.callback!({ action: "cancel" });
    await result;

    expect(state.requests).toBe(2);
  });
});

describe("carga de las bibliotecas de Google", () => {
  afterEach(() => {
    document.head.innerHTML = "";
    delete (window as unknown as { gapi?: unknown }).gapi;
  });

  function settleScripts(event: "load" | "error") {
    document.head.querySelectorAll("script").forEach((script) => {
      script.dispatchEvent(new Event(event));
    });
  }

  it("carga solo los scripts oficiales de Google y el módulo picker", async () => {
    const load = vi.fn((_name: string, options: { callback: () => void }) =>
      options.callback(),
    );
    (window as unknown as { gapi: unknown }).gapi = { load };

    const loading = loadGoogleApis();
    expect(
      [...document.head.querySelectorAll("script")].map((s) => s.src).sort(),
    ).toEqual([
      "https://accounts.google.com/gsi/client",
      "https://apis.google.com/js/api.js",
    ]);
    settleScripts("load");
    await loading;

    expect(load).toHaveBeenCalledWith("picker", expect.anything());
  });

  it("informa si no se pudieron cargar", async () => {
    const loading = loadGoogleApis();
    settleScripts("error");

    await expect(loading).rejects.toMatchObject({ code: "load_failed" });
  });
});
