import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { PickerError, type PickedFolder } from "./google/picker";

const ACCOUNT = { sub: "sub-a", email: "prueba@example.com", display_name: "Prueba" };
const CSRF = "token-csrf-de-prueba";

type Connection = {
  state: string;
  message?: string | null;
  error?: string | null;
  account?: typeof ACCOUNT | null;
};

function statusBody(connection: Connection, environment = "development", packaged = false) {
  return {
    environment,
    packaged,
    csrf_token: CSRF,
    connection: { message: null, error: null, account: null, ...connection },
    picker: { client_id: "123-web", api_key: "clave", project_number: "123" },
  };
}

type Call = { url: string; init?: RequestInit };

function mockApi(
  initial: Connection,
  routes: Record<string, () => { status?: number; body: unknown }> = {},
  environment = "development",
  packaged = false,
) {
  const calls: Call[] = [];
  const server = { connection: initial };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const route = routes[url];
      const reply = route
        ? route()
        : { body: statusBody(server.connection, environment, packaged) };
      return {
        ok: (reply.status ?? 200) < 400,
        status: reply.status ?? 200,
        json: async () => reply.body,
      };
    }),
  );

  return { calls, server };
}

function fakePicker(results: (PickedFolder | null | PickerError)[] = []) {
  const queue = [...results];
  const instance = {
    pick: vi.fn(async () => {
      const next = queue.shift();
      if (next instanceof PickerError) throw next;
      return next ?? null;
    }),
    forget: vi.fn(),
  };
  const create = vi.fn(() => instance);
  return { instance, create };
}

const folder = (id: string, name = id): PickedFolder => ({ id, name });
const noLoad = async () => {};

function renderApp(pickerResults: (PickedFolder | null | PickerError)[] = []) {
  const picker = fakePicker(pickerResults);
  render(<App createPicker={picker.create} loadApis={noLoad} pollMs={5} />);
  return picker;
}

async function enabledButton(name: RegExp) {
  const button = (await screen.findByRole("button", { name })) as HTMLButtonElement;
  // Picker se habilita cuando cargan las bibliotecas de Google.
  await waitFor(() => expect(button.disabled).toBe(false));
  return button;
}

const posts = (calls: Call[]) => calls.filter((c) => c.init?.method === "POST");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("estado de conexión", () => {
  it("sin conexión ofrece conectar y oculta las carpetas", async () => {
    mockApi({ state: "disconnected" });
    renderApp();

    expect(await screen.findByRole("button", { name: /conectar google drive/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /elegir carpeta de origen/i })).toBeNull();
  });

  it("conectada muestra la cuenta y permite desconectar", async () => {
    mockApi({ state: "connected", account: ACCOUNT });
    renderApp();

    expect(await screen.findByText("prueba@example.com")).toBeTruthy();
    expect(screen.getByRole("button", { name: /desconectar/i })).toBeTruthy();
  });

  it("indica el entorno de desarrollo, y no el de producción", async () => {
    mockApi({ state: "disconnected" });
    renderApp();
    expect(await screen.findByText(/desarrollo/i)).toBeTruthy();
    cleanup();

    mockApi({ state: "disconnected" }, {}, "production");
    renderApp();
    await screen.findByRole("button", { name: /conectar google drive/i });
    expect(screen.queryByText(/desarrollo/i)).toBeNull();
  });

  it("no indica desarrollo en el programa empaquetado, que recibe otra persona", async () => {
    mockApi({ state: "disconnected" }, {}, "development", true);
    renderApp();
    await screen.findByRole("button", { name: /conectar google drive/i });
    expect(screen.queryByText(/desarrollo/i)).toBeNull();
  });

  it("muestra el aviso si hay que reconectar", async () => {
    mockApi({
      state: "reconnect_required",
      message: "Hay que volver a conectar la cuenta.",
      account: ACCOUNT,
    });
    renderApp();

    expect(await screen.findByText("Hay que volver a conectar la cuenta.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /volver a conectar/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /elegir carpeta de origen/i })).toBeNull();
  });

  it("muestra un error si el servidor local no responde", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("sin red"); }));
    renderApp();

    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("conexión y desconexión", () => {
  it("conecta con el token CSRF y sigue el estado hasta terminar", async () => {
    const { calls, server } = mockApi(
      { state: "disconnected" },
      {
        "/api/connection/start": () => {
          server.connection = { state: "authorizing_read" };
          return {
            body: { state: "authorizing_read", account: null, message: null, error: null },
          };
        },
      },
    );
    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: /conectar google drive/i }));
    expect(await screen.findByText(/en el navegador/i)).toBeTruthy();

    const [post] = posts(calls);
    expect(post.url).toBe("/api/connection/start");
    expect((post.init!.headers as Record<string, string>)["X-CSRF-Token"]).toBe(CSRF);

    server.connection = { state: "connected", account: ACCOUNT };
    expect(await screen.findByText("prueba@example.com")).toBeTruthy();
  });

  it("permite cancelar una autorización en curso", async () => {
    const { calls } = mockApi(
      { state: "authorizing_write" },
      {
        "/api/connection/cancel": () => ({
          body: { state: "disconnected", account: null, message: null, error: null },
        }),
      },
    );
    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: /cancelar/i }));

    await waitFor(() =>
      expect(posts(calls).map((c) => c.url)).toEqual(["/api/connection/cancel"]),
    );
  });

  it("muestra el motivo si la autorización falla", async () => {
    mockApi({
      state: "disconnected",
      error: "account_mismatch",
      message: "Las dos autorizaciones deben ser de la misma cuenta de Google.",
    });
    renderApp();

    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByText(/misma cuenta de google/i)).toBeTruthy();
  });

  it("al desconectar olvida el token del navegador y las carpetas", async () => {
    const { calls, server } = mockApi(
      { state: "connected", account: ACCOUNT },
      {
        "/api/connection/disconnect": () => {
          server.connection = { state: "disconnected" };
          return { body: { state: "disconnected", account: null, message: null, error: null } };
        },
      },
    );
    const picker = renderApp([folder("origen", "Origen")]);

    fireEvent.click(await enabledButton(/elegir carpeta de origen/i));
    await screen.findAllByText("Origen");

    fireEvent.click(screen.getByRole("button", { name: /desconectar/i }));

    await screen.findByRole("button", { name: /conectar google drive/i });
    expect(posts(calls).map((c) => c.url)).toEqual(["/api/connection/disconnect"]);
    expect(picker.instance.forget).toHaveBeenCalled();
    expect(screen.queryByText("Origen")).toBeNull();
  });

  it("usa la cuenta conectada como identidad esperada del Picker", async () => {
    mockApi({ state: "connected", account: ACCOUNT });
    const picker = renderApp();

    await screen.findByRole("button", { name: /elegir carpeta de origen/i });

    expect(picker.create).toHaveBeenCalledWith(
      expect.objectContaining({ client_id: "123-web" }),
      "sub-a",
    );
  });
});

describe("selección de carpetas", () => {
  async function pickBoth(picker: ReturnType<typeof renderApp>) {
    fireEvent.click(await enabledButton(/elegir carpeta de origen/i));
    await screen.findAllByText("Origen");
    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de destino/i }));
    await screen.findAllByText("Destino");
    return picker;
  }

  const verified = (same = false) => ({
    body: {
      source: { id: "origen", name: "Origen" },
      target: { id: same ? "origen" : "destino", name: same ? "Origen" : "Destino" },
      same_folder: same,
    },
  });

  it("envía al backend únicamente los ID de las carpetas", async () => {
    const { calls } = mockApi(
      { state: "connected", account: ACCOUNT },
      { "/api/folders/verify": () => verified() },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));

    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/verificadas/i);

    const [post] = posts(calls);
    expect(post.url).toBe("/api/folders/verify");
    expect(JSON.parse(post.init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "destino",
    });
  });

  it("nunca envía el token del navegador al backend", async () => {
    const { calls } = mockApi(
      { state: "connected", account: ACCOUNT },
      { "/api/folders/verify": () => verified() },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/verificadas/i);

    const dumped = JSON.stringify(calls.map((c) => c.init ?? null));
    expect(dumped).not.toMatch(/access_token|Bearer|Authorization/i);
  });

  it("no permite verificar hasta tener las dos carpetas", async () => {
    mockApi({ state: "connected", account: ACCOUNT });
    renderApp([folder("origen", "Origen")]);

    const verify = await screen.findByRole("button", { name: /verificar carpetas/i });
    expect((verify as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de origen/i }));
    await screen.findAllByText("Origen");
    expect((verify as HTMLButtonElement).disabled).toBe(true);
  });

  it("con la misma carpeta se elige una sola vez y se envía como ambas", async () => {
    const { calls } = mockApi(
      { state: "connected", account: ACCOUNT },
      { "/api/folders/verify": () => verified(true) },
    );
    const picker = renderApp([folder("origen", "Origen")]);

    fireEvent.click(await screen.findByLabelText(/misma carpeta/i));
    expect(screen.queryByRole("button", { name: /elegir carpeta de destino/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de origen/i }));
    await screen.findAllByText("Origen");
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/misma carpeta para originales y resultados/i);

    expect(picker.instance.pick).toHaveBeenCalledTimes(1);
    expect(JSON.parse(posts(calls)[0].init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "origen",
    });
  });

  it("la misma carpeta reemplaza un destino elegido antes", async () => {
    const { calls } = mockApi(
      { state: "connected", account: ACCOUNT },
      { "/api/folders/verify": () => verified(true) },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));

    fireEvent.click(screen.getByLabelText(/misma carpeta/i));
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/misma carpeta para originales y resultados/i);

    expect(JSON.parse(posts(calls)[0].init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "origen",
    });
  });

  it("al conectar otra cuenta no quedan las carpetas de la anterior", async () => {
    const { server } = mockApi(
      { state: "connected", account: ACCOUNT },
      {
        "/api/connection/disconnect": () => {
          server.connection = { state: "disconnected" };
          return { body: { state: "disconnected", account: null, message: null, error: null } };
        },
        "/api/connection/start": () => {
          server.connection = {
            state: "connected",
            account: { ...ACCOUNT, sub: "sub-b", email: "otra@example.com" },
          };
          return { body: { ...server.connection, message: null, error: null } };
        },
        "/api/folders/verify": () => verified(true),
      },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));
    fireEvent.click(screen.getByLabelText(/misma carpeta/i));
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/misma carpeta para originales/i);

    fireEvent.click(screen.getByRole("button", { name: /desconectar/i }));
    fireEvent.click(await screen.findByRole("button", { name: /conectar google drive/i }));
    await screen.findByText("otra@example.com");

    expect(screen.queryByText("Origen")).toBeNull();
    expect(screen.queryByText("Destino")).toBeNull();
    expect(screen.queryByText(/misma carpeta para originales/i)).toBeNull();
    expect((screen.getByLabelText(/misma carpeta/i) as HTMLInputElement).checked).toBe(false);
    // En los dos lugares del panel y en el recorrido de la pantalla.
    expect(screen.getAllByText("Sin elegir")).toHaveLength(4);
  });

  it("cancelar el Picker no cambia la selección ni llama al backend", async () => {
    const { calls } = mockApi({ state: "connected", account: ACCOUNT });
    renderApp([folder("origen", "Origen"), null]);

    const button = await screen.findByRole("button", { name: /elegir carpeta de origen/i });
    fireEvent.click(button);
    await screen.findAllByText("Origen");
    fireEvent.click(screen.getByRole("button", { name: /origen/i, description: "" }));

    await waitFor(() => expect(screen.getAllByText("Origen").length).toBeGreaterThan(0));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(posts(calls)).toEqual([]);
  });

  it("con otra cuenta en el Picker muestra el error y no elige nada", async () => {
    const { calls } = mockApi({ state: "connected", account: ACCOUNT });
    renderApp([new PickerError("account_mismatch", "Eligió otra cuenta de Google.")]);

    fireEvent.click(await enabledButton(/elegir carpeta de origen/i));

    expect((await screen.findByRole("alert")).textContent).toMatch(/otra cuenta/i);
    expect(posts(calls)).toEqual([]);
    expect((screen.getByRole("button", { name: /verificar carpetas/i }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("muestra el error del backend y no da por verificadas las carpetas", async () => {
    mockApi(
      { state: "connected", account: ACCOUNT },
      {
        "/api/folders/verify": () => ({
          status: 409,
          body: { error: { code: "target_not_authorized", message: "No puede crear archivos." } },
        }),
      },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));

    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/no puede crear archivos/i);
    expect(screen.queryByText(/verificadas/i)).toBeNull();
  });

  it("cambiar una carpeta invalida la verificación anterior", async () => {
    mockApi(
      { state: "connected", account: ACCOUNT },
      { "/api/folders/verify": () => verified() },
    );
    await pickBoth(
      renderApp([
        folder("origen", "Origen"),
        folder("destino", "Destino"),
        folder("otra", "Otra"),
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/verificadas/i);

    fireEvent.click(screen.getByRole("button", { name: /cambiar destino/i }));
    await screen.findAllByText("Otra");

    expect(screen.queryByText(/verificadas/i)).toBeNull();
  });

  it("si la autorización expiró, vuelve a pedir la conexión", async () => {
    const { server } = mockApi(
      { state: "connected", account: ACCOUNT },
      {
        "/api/folders/verify": () => {
          server.connection = { state: "reconnect_required", account: ACCOUNT };
          return {
            status: 401,
            body: { error: { code: "reconnect_required", message: "La autorización expiró." } },
          };
        },
      },
    );
    await pickBoth(renderApp([folder("origen", "Origen"), folder("destino", "Destino")]));

    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));

    expect(await screen.findByRole("button", { name: /volver a conectar/i })).toBeTruthy();
  });

  it("no permite abrir Picker hasta que cargaron las bibliotecas de Google", async () => {
    mockApi({ state: "connected", account: ACCOUNT });
    const picker = fakePicker();
    let finish = () => {};
    const loading = new Promise<void>((resolve) => {
      finish = resolve;
    });
    render(<App createPicker={picker.create} loadApis={() => loading} pollMs={5} />);

    const button = (await screen.findByRole("button", {
      name: /elegir carpeta de origen/i,
    })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);

    finish();
    await waitFor(() => expect(button.disabled).toBe(false));
  });

  it("informa si no se pudieron cargar las bibliotecas de Google", async () => {
    mockApi({ state: "connected", account: ACCOUNT });
    const picker = fakePicker();
    render(
      <App
        createPicker={picker.create}
        loadApis={async () => {
          throw new PickerError("load_failed", "No se pudo cargar Google Picker.");
        }}
        pollMs={5}
      />,
    );

    expect((await screen.findByRole("alert")).textContent).toMatch(/no se pudo cargar/i);
  });
});
