import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Job, JobItem } from "./api";

const ACCOUNT = { sub: "sub-a", email: "prueba@example.com", display_name: "Prueba" };
const CSRF = "token-csrf-de-prueba";
const JOB_ID = "a".repeat(32);
const OUTPUT = {
  width: 400,
  height: 400,
  background: "white",
  backgrounds: ["white", "transparent"],
  min_side: 64,
  max_side: 6000,
  max_pixels: 16_000_000,
};
const DEFAULT_OPTIONS = { width: 400, height: 400, background: "white" };

function item(overrides: Partial<JobItem> = {}): JobItem {
  return {
    file_id: "foto1",
    name: "foto1.jpg",
    status: "planned",
    stage: "download",
    reason: null,
    actions: ["download"],
    undetermined: ["processing", "upload"],
    notes: [],
    related: [],
    ...overrides,
  };
}

function summary(overrides: Partial<NonNullable<Job["summary"]>> = {}): NonNullable<Job["summary"]> {
  return {
    counts: { complete: 0, planned: 1, failed: 0, pending: 0, not_started: 0 },
    uploaded_now: 0,
    complete: true,
    can_run: true,
    stopped: null,
    warnings: [],
    items: [item()],
    ...overrides,
  };
}

function job(overrides: Partial<Job> = {}): Job {
  return {
    id: JOB_ID,
    kind: "preview",
    status: "finished",
    source: { id: "origen", name: "Originales" },
    target: { id: "destino", name: "Resultados" },
    same_folder: false,
    created_at: 1,
    finished_at: 2,
    progress: {
      total: null,
      stage: null,
      current: null,
      download: { done: 0, failed: 0 },
      processing: { done: 0, failed: 0 },
      upload: { done: 0, failed: 0 },
    },
    summary: summary(),
    error: null,
    options: DEFAULT_OPTIONS,
    ...overrides,
  };
}

type Call = { url: string; init?: RequestInit };

function mockApi(initial: {
  connection?: Record<string, unknown>;
  current?: Job | null;
  routes?: Record<string, () => { status?: number; body: unknown }>;
}) {
  const calls: Call[] = [];
  const server = {
    connection: { state: "connected", account: ACCOUNT, message: null, error: null, ...initial.connection },
    current: initial.current ?? null,
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const route = initial.routes?.[url];
      const reply =
        route?.() ??
        (url === "/api/jobs/current"
          ? { body: { job: server.current } }
          : {
              body: {
                environment: "development",
                csrf_token: CSRF,
                connection: server.connection,
                picker: { client_id: "123-web", api_key: "clave", project_number: "123" },
                output: OUTPUT,
              },
            });
      return {
        ok: (reply.status ?? 200) < 400,
        status: reply.status ?? 200,
        json: async () => reply.body,
      };
    }),
  );

  return { calls, server };
}

const posts = (calls: Call[]) => calls.filter((c) => c.init?.method === "POST");

function renderApp(results: ({ id: string; name: string } | null)[] = []) {
  const queue = [...results];
  const instance = {
    pick: vi.fn(async () => queue.shift() ?? null),
    forget: vi.fn(),
  };
  render(<App createPicker={() => instance} loadApis={async () => {}} pollMs={5} />);
  return instance;
}

async function enabledButton(name: RegExp) {
  const button = (await screen.findByRole("button", { name })) as HTMLButtonElement;
  // Picker se habilita cuando cargan las bibliotecas de Google.
  await waitFor(() => expect(button.disabled).toBe(false));
  return button;
}

async function selectAndVerify() {
  fireEvent.click(await enabledButton(/elegir carpeta de origen/i));
  await screen.findByText("Origen");
  fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de destino/i }));
  await screen.findByText("Destino");
  fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
  await screen.findByText(/verificadas/i);
}

const verified = () => ({
  body: {
    source: { id: "origen", name: "Origen" },
    target: { id: "destino", name: "Destino" },
    same_folder: false,
  },
});

const folders = [
  { id: "origen", name: "Origen" },
  { id: "destino", name: "Destino" },
];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("vista previa", () => {
  it("solo se ofrece después de verificar las carpetas", async () => {
    mockApi({ routes: { "/api/folders/verify": verified } });
    renderApp(folders);

    await screen.findByRole("button", { name: /elegir carpeta de origen/i });
    expect(screen.queryByRole("button", { name: /ver vista previa/i })).toBeNull();

    await selectAndVerify();

    expect(screen.getByRole("button", { name: /ver vista previa/i })).toBeTruthy();
  });

  it("envía solo los ID y muestra el plan", async () => {
    const { calls } = mockApi({
      routes: {
        "/api/folders/verify": verified,
        "/api/jobs/preview": () => ({ body: job() }),
      },
    });
    renderApp(folders);
    await selectAndVerify();

    fireEvent.click(screen.getByRole("button", { name: /ver vista previa/i }));

    expect(await screen.findByText("foto1.jpg")).toBeTruthy();
    const post = posts(calls).find((c) => c.url === "/api/jobs/preview")!;
    expect(JSON.parse(post.init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "destino",
      options: DEFAULT_OPTIONS,
    });
    expect((post.init!.headers as Record<string, string>)["X-CSRF-Token"]).toBe(CSRF);
    expect(screen.getByText("Se procesa")).toBeTruthy();
    expect(screen.getByText(/no se modifican ni se borran/i)).toBeTruthy();
  });

  it("con la misma carpeta envía el mismo ID como origen y destino", async () => {
    const { calls } = mockApi({
      routes: {
        "/api/folders/verify": () => ({
          body: {
            source: { id: "origen", name: "Origen" },
            target: { id: "origen", name: "Origen" },
            same_folder: true,
          },
        }),
        "/api/jobs/preview": () => ({ body: job({ same_folder: true }) }),
      },
    });
    renderApp([folders[0]]);

    fireEvent.click(await screen.findByLabelText(/misma carpeta/i));
    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de origen/i }));
    await screen.findByText("Origen");
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/misma carpeta para originales/i);
    fireEvent.click(screen.getByRole("button", { name: /ver vista previa/i }));
    await screen.findByText("foto1.jpg");

    const post = posts(calls).find((c) => c.url === "/api/jobs/preview")!;
    expect(JSON.parse(post.init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "origen",
      options: DEFAULT_OPTIONS,
    });
  });

  it("la misma carpeta reemplaza un destino elegido antes", async () => {
    const { calls } = mockApi({
      routes: {
        "/api/folders/verify": () => ({
          body: {
            source: { id: "origen", name: "Origen" },
            target: { id: "origen", name: "Origen" },
            same_folder: true,
          },
        }),
        "/api/jobs/preview": () => ({ body: job({ same_folder: true }) }),
      },
    });
    renderApp(folders);

    fireEvent.click(await enabledButton(/elegir carpeta de origen/i));
    await screen.findByText("Origen");
    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de destino/i }));
    await screen.findByText("Destino");
    fireEvent.click(screen.getByLabelText(/misma carpeta/i));
    fireEvent.click(screen.getByRole("button", { name: /verificar carpetas/i }));
    await screen.findByText(/misma carpeta para originales/i);
    fireEvent.click(screen.getByRole("button", { name: /ver vista previa/i }));
    await screen.findByText("foto1.jpg");

    const post = posts(calls).find((c) => c.url === "/api/jobs/preview")!;
    expect(JSON.parse(post.init!.body as string)).toEqual({
      source_id: "origen",
      target_id: "origen",
      options: DEFAULT_OPTIONS,
    });
  });

  it("muestra el motivo de una foto que no se puede procesar", async () => {
    mockApi({
      current: job({
        summary: summary({
          counts: { complete: 0, planned: 0, failed: 1, pending: 0, not_started: 0 },
          can_run: false,
          complete: false,
          items: [item({ status: "failed", reason: "es demasiado grande" })],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText(/es demasiado grande/)).toBeTruthy();
  });

  it("avisa cuando no hay nada que procesar y no ofrece ejecutar", async () => {
    mockApi({
      current: job({
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 0, pending: 0, not_started: 0 },
          can_run: false,
          items: [item({ status: "complete", actions: [], undetermined: [] })],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText(/no hay nada que procesar/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /procesar/i })).toBeNull();
  });

  it("muestra las advertencias y los PNG relacionados", async () => {
    mockApi({
      current: job({
        summary: summary({
          warnings: ["Archivos de Drive no incluidos (formato): 2."],
          items: [
            item({
              notes: ["Se recuperó el registro."],
              related: [{ kind: "previous_revision", name: "TRANSFORMADA_vieja.png" }],
            }),
          ],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText(/no incluidos \(formato\)/)).toBeTruthy();
    expect(screen.getByText(/Se recuperó el registro/)).toBeTruthy();
    expect(screen.getByText(/TRANSFORMADA_vieja\.png/)).toBeTruthy();
  });
});

describe("ejecución", () => {
  it("ejecuta la vista previa mostrada, sin enviar carpetas", async () => {
    const { calls } = mockApi({
      current: job(),
      routes: {
        [`/api/jobs/${JOB_ID}/run`]: () => ({ body: job({ id: "b".repeat(32), kind: "run", status: "running", summary: null }) }),
      },
    });
    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: /procesar 1 imagen/i }));

    await waitFor(() =>
      expect(posts(calls).map((c) => c.url)).toEqual([`/api/jobs/${JOB_ID}/run`]),
    );
    const post = posts(calls)[0];
    expect(post.init!.body).toBeUndefined();
    expect((post.init!.headers as Record<string, string>)["X-CSRF-Token"]).toBe(CSRF);
  });

  it("muestra el total de imágenes a procesar, sumando lo pendiente", async () => {
    mockApi({
      current: job({
        summary: summary({
          counts: { complete: 3, planned: 2, failed: 0, pending: 1, not_started: 0 },
        }),
      }),
    });
    renderApp();

    expect(await screen.findByRole("button", { name: /procesar 3 imágenes/i })).toBeTruthy();
  });

  it("sigue el progreso hasta el resumen final", async () => {
    const running = job({
      kind: "run",
      status: "running",
      summary: null,
      progress: {
        total: 4,
        stage: "processing",
        current: "foto2.jpg",
        download: { done: 4, failed: 0 },
        processing: { done: 2, failed: 0 },
        upload: { done: 1, failed: 0 },
      },
    });
    const { server } = mockApi({ current: running });
    renderApp();

    expect(await screen.findByText(/foto2\.jpg/)).toBeTruthy();
    const progress = screen.getByRole("list", { name: /progreso/i });
    expect(within(progress).getByText(/descarga/i).parentElement!.textContent).toMatch(/4 de 4/);
    expect(within(progress).getByText(/procesamiento/i).parentElement!.textContent).toMatch(/2 de 4/);
    expect(within(progress).getByText(/subida/i).parentElement!.textContent).toMatch(/1 de 4/);

    server.current = job({
      kind: "run",
      summary: summary({
        counts: { complete: 4, planned: 0, failed: 0, pending: 0, not_started: 0 },
        uploaded_now: 3,
        can_run: false,
        items: [item({ status: "complete", actions: [], undetermined: [] })],
      }),
    });

    expect(await screen.findByText(/3 PNG subidos ahora/i)).toBeTruthy();
    expect(screen.queryByText(/foto2\.jpg/)).toBeNull();
  });

  it("muestra los fallos de cada etapa en el progreso", async () => {
    mockApi({
      current: job({
        kind: "run",
        status: "running",
        summary: null,
        progress: {
          total: 2,
          stage: "download",
          current: "foto2.jpg",
          download: { done: 2, failed: 1 },
          processing: { done: 0, failed: 0 },
          upload: { done: 0, failed: 0 },
        },
      }),
    });
    renderApp();

    const progress = await screen.findByRole("list", { name: /progreso/i });
    expect(within(progress).getByText(/descarga/i).parentElement!.textContent).toMatch(/1 con errores/);
  });

  it("avisa de la espera al preparar el procesamiento", async () => {
    mockApi({
      current: job({
        kind: "run",
        status: "running",
        summary: null,
        progress: {
          total: 2,
          stage: "download",
          current: null,
          download: { done: 2, failed: 0 },
          processing: { done: 0, failed: 0 },
          upload: { done: 0, failed: 0 },
        },
      }),
    });
    renderApp();

    expect(await screen.findByText(/modelo de ia/i)).toBeTruthy();
  });

  it("muestra el resumen con errores por foto", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 1, pending: 1, not_started: 0 },
          uploaded_now: 1,
          complete: false,
          can_run: false,
          items: [
            item({ file_id: "a", name: "a.jpg", status: "complete" }),
            item({ file_id: "b", name: "b.jpg", status: "failed", stage: "processing", reason: "imagen dañada" }),
            item({ file_id: "c", name: "c.jpg", status: "pending", stage: "upload", reason: "no se pudo confirmar" }),
          ],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText(/imagen dañada/)).toBeTruthy();
    expect(screen.getByText(/no se pudo confirmar/)).toBeTruthy();
    expect(screen.getByText(/vuelva a generar la vista previa/i)).toBeTruthy();
  });

  it("en el resumen, lo subido figura en Drive, no como si ya estuviera", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 0, pending: 0, not_started: 0 },
          uploaded_now: 1,
          can_run: false,
          items: [item({ status: "complete", actions: [], undetermined: [] })],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText("En Drive")).toBeTruthy();
    expect(screen.queryByText("Ya está en Drive")).toBeNull();
  });

  it("en la vista previa, lo hecho antes figura como ya en Drive", async () => {
    mockApi({
      current: job({
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 0, pending: 0, not_started: 0 },
          can_run: false,
          items: [item({ status: "complete", actions: [], undetermined: [] })],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText("Ya está en Drive")).toBeTruthy();
  });

  it("usa el singular cuando corresponde", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 0, pending: 1, not_started: 1 },
          uploaded_now: 1,
          complete: false,
          can_run: false,
          items: [],
        }),
      }),
    });
    renderApp();

    expect(await screen.findByText("1 PNG subido ahora")).toBeTruthy();
    expect(screen.getByText("1 pendiente de confirmar")).toBeTruthy();
    expect(screen.getByText("1 no iniciada")).toBeTruthy();
  });

  it("muestra el error que detuvo la ejecución", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 0, planned: 0, failed: 0, pending: 0, not_started: 2 },
          complete: false,
          can_run: false,
          stopped: { stage: "upload", code: "quota_exceeded", message: "Se agotó la cuota." },
          items: [item({ status: "not_started", stage: "upload" })],
        }),
      }),
    });
    renderApp();

    expect((await screen.findByRole("alert")).textContent).toMatch(/se agotó la cuota/i);
  });

  it("muestra un trabajo fallido", async () => {
    mockApi({
      current: job({
        status: "failed",
        summary: null,
        error: { code: "target_not_authorized", message: "No puede crear archivos." },
      }),
    });
    renderApp();

    expect((await screen.findByRole("alert")).textContent).toMatch(/no puede crear archivos/i);
    expect(screen.queryByRole("button", { name: /procesar/i })).toBeNull();
  });

  it("muestra un trabajo interrumpido", async () => {
    mockApi({
      current: job({
        kind: "run",
        status: "interrupted",
        summary: null,
        error: { code: "interrupted", message: "Se cerró la aplicación." },
      }),
    });
    renderApp();

    expect((await screen.findByRole("alert")).textContent).toMatch(/se cerró la aplicación/i);
  });

  it("si el servidor rechaza la ejecución muestra el motivo", async () => {
    mockApi({
      current: job(),
      routes: {
        [`/api/jobs/${JOB_ID}/run`]: () => ({
          status: 409,
          body: { error: { code: "preview_expired", message: "La vista previa venció." } },
        }),
      },
    });
    renderApp();

    fireEvent.click(await screen.findByRole("button", { name: /procesar 1 imagen/i }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/venció/);
  });
});

describe("trabajo en curso", () => {
  const running = () =>
    job({
      kind: "run",
      status: "running",
      summary: null,
      progress: {
        total: 1,
        stage: "download",
        current: "foto1.jpg",
        download: { done: 0, failed: 0 },
        processing: { done: 0, failed: 0 },
        upload: { done: 0, failed: 0 },
      },
    });

  it("se recupera al volver a abrir la página", async () => {
    const { calls } = mockApi({ current: running() });
    renderApp();

    expect(await screen.findByText(/foto1\.jpg/)).toBeTruthy();
    expect(calls.some((c) => c.url === "/api/jobs/current")).toBe(true);
  });

  it("bloquea desconectar y cambiar las carpetas", async () => {
    mockApi({ current: running() });
    renderApp();

    await screen.findByText(/foto1\.jpg/);

    expect((screen.getByRole("button", { name: /desconectar/i }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => screen.getByRole("button", { name: /elegir carpeta de origen/i }));
    expect((screen.getByRole("button", { name: /elegir carpeta de origen/i }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByLabelText(/misma carpeta/i)).toHaveProperty("disabled", true);
  });

  it("no permite iniciar otra vista previa", async () => {
    const { server } = mockApi({
      routes: {
        "/api/folders/verify": verified,
        "/api/jobs/preview": () => {
          server.current = running();
          return { body: running() };
        },
      },
    });
    renderApp(folders);
    await selectAndVerify();

    fireEvent.click(screen.getByRole("button", { name: /ver vista previa/i }));
    await screen.findByText(/foto1\.jpg/);

    expect(screen.queryByRole("button", { name: /ver vista previa/i })).toBeNull();
    expect(
      (screen.getByRole("button", { name: /verificar carpetas/i }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
});

describe("relación con las carpetas", () => {
  it("cambiar una carpeta oculta la vista previa anterior", async () => {
    mockApi({
      current: job(),
      routes: { "/api/folders/verify": verified },
    });
    renderApp([{ id: "otra", name: "Otra" }]);

    await screen.findByText("foto1.jpg");
    fireEvent.click(screen.getByRole("button", { name: /elegir carpeta de origen/i }));
    await screen.findByText("Otra");

    expect(screen.queryByText("foto1.jpg")).toBeNull();
    expect(screen.queryByRole("button", { name: /procesar/i })).toBeNull();
  });

  it("muestra los nombres de las carpetas del trabajo", async () => {
    mockApi({ current: job() });
    renderApp();

    expect(await screen.findByText(/Originales/)).toBeTruthy();
    expect(screen.getByText(/Resultados/)).toBeTruthy();
  });

  it("no pide el trabajo si no hay cuenta conectada", async () => {
    const { calls } = mockApi({ connection: { state: "disconnected", account: null } });
    renderApp();

    await screen.findByRole("button", { name: /conectar google drive/i });

    expect(calls.some((c) => c.url === "/api/jobs/current")).toBe(false);
  });

  it("nunca envía tokens del navegador", async () => {
    const { calls } = mockApi({
      routes: {
        "/api/folders/verify": verified,
        "/api/jobs/preview": () => ({ body: job() }),
      },
    });
    renderApp(folders);
    await selectAndVerify();
    fireEvent.click(screen.getByRole("button", { name: /ver vista previa/i }));
    await screen.findByText("foto1.jpg");

    expect(JSON.stringify(calls.map((c) => c.init ?? null))).not.toMatch(
      /access_token|Bearer|Authorization/i,
    );
  });
});

describe("tamaño y fondo del resultado", () => {
  async function ready() {
    const { calls } = mockApi({
      routes: {
        "/api/folders/verify": verified,
        "/api/jobs/preview": () => ({ body: job() }),
      },
    });
    renderApp(folders);
    await selectAndVerify();
    return calls;
  }

  const width = () => screen.getByLabelText(/ancho/i) as HTMLInputElement;
  const height = () => screen.getByLabelText(/alto/i) as HTMLInputElement;
  const previewButton = () =>
    screen.getByRole("button", { name: /ver vista previa/i }) as HTMLButtonElement;

  it("por defecto es 400 x 400 con fondo blanco", async () => {
    await ready();

    expect(width().value).toBe("400");
    expect(height().value).toBe("400");
    expect((screen.getByLabelText(/blanco/i) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText(/transparente/i) as HTMLInputElement).checked).toBe(false);
  });

  it("envía el tamaño y el fondo elegidos con la vista previa", async () => {
    const calls = await ready();

    fireEvent.change(width(), { target: { value: "800" } });
    fireEvent.change(height(), { target: { value: "600" } });
    fireEvent.click(screen.getByLabelText(/transparente/i));
    fireEvent.click(previewButton());
    await screen.findByText("foto1.jpg");

    const post = posts(calls).find((c) => c.url === "/api/jobs/preview")!;
    expect(JSON.parse(post.init!.body as string).options).toEqual({
      width: 800,
      height: 600,
      background: "transparent",
    });
  });

  it.each([
    ["un lado menor que el mínimo", "63", "400", /entre 64 y 6000/],
    ["un lado mayor que el máximo", "400", "6001", /entre 64 y 6000/],
    ["un número con decimales", "400.5", "400", /número entero/],
    ["un campo vacío", "", "400", /número entero/],
    ["demasiados píxeles", "5000", "5000", /16\.000\.000|16000000|16,000,000/],
  ])("no permite la vista previa con %s", async (_name, w, h, message) => {
    const calls = await ready();

    fireEvent.change(width(), { target: { value: w } });
    fireEvent.change(height(), { target: { value: h } });

    expect(previewButton().disabled).toBe(true);
    expect(screen.getByText(message)).toBeTruthy();
    fireEvent.click(previewButton());
    expect(posts(calls).some((c) => c.url === "/api/jobs/preview")).toBe(false);
  });

  it("cambiar una opción oculta la vista previa anterior", async () => {
    await ready();
    fireEvent.click(previewButton());
    await screen.findByText("foto1.jpg");

    fireEvent.click(screen.getByLabelText(/transparente/i));

    expect(screen.queryByText("foto1.jpg")).toBeNull();
    expect(screen.queryByRole("button", { name: /procesar/i })).toBeNull();
  });

  it("no se pueden cambiar mientras hay un trabajo en curso", async () => {
    mockApi({
      current: job({ kind: "run", status: "running", summary: null }),
    });
    renderApp();

    await waitFor(() => expect(width().disabled).toBe(true));
    expect(height().disabled).toBe(true);
    expect((screen.getByLabelText(/transparente/i) as HTMLInputElement).disabled).toBe(true);
  });

  it("muestra las opciones del trabajo", async () => {
    mockApi({
      current: job({ options: { width: 800, height: 600, background: "transparent" } }),
    });
    renderApp();

    expect(await screen.findByText(/800 × 600 px, fondo transparente/)).toBeTruthy();
  });

  it("un trabajo guardado sin opciones se muestra igual", async () => {
    const old = job();
    delete (old as Partial<Job>).options;
    mockApi({ current: old });
    renderApp();

    expect(await screen.findByText("foto1.jpg")).toBeTruthy();
  });

  it("avisa que otras opciones crean resultados nuevos", async () => {
    await ready();

    expect(screen.getByText(/se crean resultados nuevos/i)).toBeTruthy();
  });
});

describe("presentación del resumen y del progreso", () => {
  it("muestra primero las imágenes con problemas", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 1, planned: 0, failed: 1, pending: 0, not_started: 0 },
          complete: false,
          can_run: false,
          items: [
            item({ file_id: "a", name: "bien.jpg", status: "complete" }),
            item({ file_id: "b", name: "mal.jpg", status: "failed", reason: "dañada" }),
          ],
        }),
      }),
    });
    renderApp();

    const bad = await screen.findByText("mal.jpg");
    const good = screen.getByText("bien.jpg");
    expect(bad.compareDocumentPosition(good) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("agrupa muchas imágenes sin problemas y deja los errores a la vista", async () => {
    const ok = Array.from({ length: 8 }, (_, i) =>
      item({ file_id: `ok${i}`, name: `ok${i}.jpg`, status: "complete" }),
    );
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          counts: { complete: 8, planned: 0, failed: 1, pending: 0, not_started: 0 },
          complete: false,
          can_run: false,
          items: [...ok, item({ file_id: "x", name: "mal.jpg", status: "failed" })],
        }),
      }),
    });
    renderApp();

    const group = (await screen.findByText(/ver las 8 imágenes sin problemas/i)).closest("details")!;
    expect(group.open).toBe(false);
    expect(within(group).getByText("ok0.jpg")).toBeTruthy();
    expect(group.contains(screen.getByText("mal.jpg"))).toBe(false);
  });

  it("pocas imágenes sin problemas no se agrupan", async () => {
    mockApi({ current: job() });
    renderApp();

    await screen.findByText("foto1.jpg");
    expect(screen.queryByText(/sin problemas/i)).toBeNull();
  });

  it("la barra muestra el avance total de las tres etapas", async () => {
    mockApi({
      current: job({
        kind: "run",
        status: "running",
        summary: null,
        progress: {
          total: 4,
          stage: "processing",
          current: "foto2.jpg",
          download: { done: 4, failed: 0 },
          processing: { done: 2, failed: 0 },
          upload: { done: 1, failed: 0 },
        },
      }),
    });
    renderApp();

    const bar = await screen.findByRole("progressbar", { name: /avance total/i });
    expect(bar.getAttribute("aria-valuenow")).toBe("58");
    expect(screen.getByText("58 %")).toBeTruthy();
  });

  it("sin total conocido la barra no inventa un porcentaje", async () => {
    mockApi({ current: job({ status: "running", summary: null }) });
    renderApp();

    const bar = await screen.findByRole("progressbar", { name: /avance total/i });
    expect(bar.getAttribute("aria-valuenow")).toBeNull();
    expect(screen.queryByText(/ %$/)).toBeNull();
  });

  it("el resumen dice si terminó bien o si hay que revisar", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({ uploaded_now: 1, can_run: false, items: [item({ status: "complete" })] }),
      }),
    });
    renderApp();
    expect(await screen.findByText(/listo: todo quedó en drive/i)).toBeTruthy();
    cleanup();

    mockApi({
      current: job({
        kind: "run",
        summary: summary({ complete: false, can_run: false, items: [item({ status: "failed" })] }),
      }),
    });
    renderApp();
    expect(await screen.findByText(/hay imágenes para revisar/i)).toBeTruthy();
  });
});

describe("pasos", () => {
  it("sin conexión, la bienvenida explica los pasos y no muestra controles", async () => {
    mockApi({ connection: { state: "disconnected", account: null } });
    renderApp();

    await screen.findByRole("button", { name: /conectar google drive/i });
    const steps = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(steps).toEqual([
      "Conecte su cuenta de Google.",
      "Elija la carpeta de las fotos y dónde guardar los resultados.",
      "Revise la vista previa y confirme el procesamiento.",
    ]);
    expect(screen.queryByLabelText(/ancho/i)).toBeNull();
    expect(screen.queryByText(/verifique las carpetas para ver/i)).toBeNull();
  });

  it("la vista previa espera a que se verifiquen las carpetas", async () => {
    mockApi({ routes: { "/api/folders/verify": verified } });
    renderApp(folders);

    // Con la cuenta ya conectada: antes, todos los pasos están bloqueados.
    await screen.findByRole("button", { name: /elegir carpeta de origen/i });
    expect(screen.getByText(/verifique las carpetas para ver/i)).toBeTruthy();
    await selectAndVerify();

    expect(screen.queryByText(/verifique las carpetas para ver/i)).toBeNull();
    expect(screen.getByText(/nada se modifica hasta que confirme/i)).toBeTruthy();
  });

  it("con un trabajo guardado, el último paso se muestra sin volver a verificar", async () => {
    mockApi({ current: job() });
    renderApp();

    expect(await screen.findByText("foto1.jpg")).toBeTruthy();
    expect(screen.queryByText(/verifique las carpetas para ver/i)).toBeNull();
  });

  it("los pasos completos se marcan", async () => {
    mockApi({ routes: { "/api/folders/verify": verified } });
    renderApp(folders);
    await selectAndVerify();

    const folderStep = screen.getByRole("region", { name: "Carpetas" });
    expect(folderStep.className).toMatch(/done/);
    const accountStep = screen.getByRole("region", { name: "Cuenta de Google Drive" });
    expect(accountStep.className).toMatch(/done/);
    expect(screen.getByRole("region", { name: "Resultado" }).className).toMatch(/done/);
  });

  it("el resultado no figura como completo antes de verificar las carpetas", async () => {
    mockApi({});
    renderApp();

    await screen.findByLabelText(/ancho/i);
    expect(screen.getByRole("region", { name: "Resultado" }).className).not.toMatch(/done/);
  });
});

describe("nombre de la aplicación", () => {
  it("se llama DIPS, Drive Image Processing Service", async () => {
    mockApi({});
    renderApp();

    expect(await screen.findByText("DIPS")).toBeTruthy();
    expect(screen.getByText("Drive Image Processing Service")).toBeTruthy();
    expect(screen.queryByText("Drive Image Processor")).toBeNull();
  });
});

describe("comparador antes y después", () => {
  const withImages = (overrides: Partial<JobItem> = {}) =>
    item({ status: "complete", images: { original: true, result: true }, ...overrides });

  it("muestra el original y el resultado de la primera foto que los tiene", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          can_run: false,
          items: [
            item({ file_id: "sin", name: "sin.jpg", status: "failed" }),
            withImages({ file_id: "f1", name: "uno.jpg" }),
          ],
        }),
      }),
    });
    renderApp();

    const original = (await screen.findByAltText("Original de uno.jpg")) as HTMLImageElement;
    const result = screen.getByAltText("Resultado de uno.jpg") as HTMLImageElement;
    expect(original.getAttribute("src")).toMatch(/^\/api\/jobs\/current\/images\/f1\/original/);
    expect(result.getAttribute("src")).toMatch(/^\/api\/jobs\/current\/images\/f1\/result/);
  });

  it("la línea se mueve con el control, también con el teclado", async () => {
    mockApi({
      current: job({ kind: "run", summary: summary({ can_run: false, items: [withImages()] }) }),
    });
    renderApp();

    const range = (await screen.findByRole("slider", {
      name: /comparar original y resultado de foto1\.jpg/i,
    })) as HTMLInputElement;
    expect(range.value).toBe("50");

    fireEvent.change(range, { target: { value: "20" } });

    const figure = range.closest("figure")!;
    expect(figure.style.getPropertyValue("--pos")).toBe("20%");
  });

  it("se puede elegir qué foto comparar", async () => {
    mockApi({
      current: job({
        kind: "run",
        summary: summary({
          can_run: false,
          items: [
            withImages({ file_id: "f1", name: "uno.jpg" }),
            withImages({ file_id: "f2", name: "dos.jpg" }),
          ],
        }),
      }),
    });
    renderApp();

    await screen.findByAltText("Original de uno.jpg");
    const buttons = screen.getAllByRole("button", { name: "Comparar" });
    fireEvent.click(buttons[1]);

    expect(await screen.findByAltText("Original de dos.jpg")).toBeTruthy();
    expect(screen.queryByAltText("Original de uno.jpg")).toBeNull();
    expect(buttons[1].getAttribute("aria-pressed")).toBe("true");
  });

  it("sin imágenes locales no hay comparador", async () => {
    mockApi({ current: job() });
    renderApp();

    await screen.findByText("foto1.jpg");
    expect(screen.queryByRole("slider")).toBeNull();
    expect(screen.queryByRole("button", { name: "Comparar" })).toBeNull();
  });
});

describe("lienzo de ejemplo", () => {
  it("muestra cómo queda el lienzo con las opciones elegidas", async () => {
    mockApi({});
    renderApp();

    const image = (await screen.findByAltText(/ejemplo de resultado de 400 × 400/i)) as HTMLImageElement;
    expect(image.getAttribute("src")).toBe("/api/sample?width=400&height=400&background=white");
  });

  it("se actualiza al cambiar el tamaño y el fondo", async () => {
    mockApi({});
    renderApp();
    await screen.findByAltText(/ejemplo de resultado/i);

    fireEvent.change(screen.getByLabelText(/ancho/i), { target: { value: "800" } });
    fireEvent.click(screen.getByLabelText(/transparente/i));

    const image = (await screen.findByAltText(
      /ejemplo de resultado de 800 × 400 px, fondo transparente/i,
    )) as HTMLImageElement;
    expect(image.getAttribute("src")).toBe(
      "/api/sample?width=800&height=400&background=transparent",
    );
  });

  it("con medidas inválidas no pide un ejemplo", async () => {
    mockApi({});
    renderApp();
    await screen.findByAltText(/ejemplo de resultado/i);

    fireEvent.change(screen.getByLabelText(/ancho/i), { target: { value: "10" } });

    await waitFor(() => expect(screen.queryByAltText(/ejemplo de resultado/i)).toBeNull());
  });

  it("sin fotos todavía, muestra un producto de muestra y lo explica", async () => {
    mockApi({});
    renderApp();

    const image = await screen.findByAltText(/ejemplo de resultado/i);
    fireEvent.error(image);

    expect(await screen.findByText(/cuando tenga fotos procesadas/i)).toBeTruthy();
  });
});

describe("panel lateral", () => {
  afterEach(() => localStorage.clear());

  const handle = () =>
    screen.getByRole("separator", { name: /redimensionar el panel lateral/i });

  it("se ensancha y se angosta con las flechas, dentro de sus límites", async () => {
    mockApi({});
    renderApp();
    await screen.findByLabelText(/ancho/i);

    expect(handle().getAttribute("aria-valuenow")).toBe("400");
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    expect(handle().getAttribute("aria-valuenow")).toBe("416");
    fireEvent.keyDown(handle(), { key: "End" });
    expect(handle().getAttribute("aria-valuenow")).toBe("600");
    fireEvent.keyDown(handle(), { key: "ArrowRight" });
    expect(handle().getAttribute("aria-valuenow")).toBe("600");
    fireEvent.keyDown(handle(), { key: "Home" });
    expect(handle().getAttribute("aria-valuenow")).toBe("300");
  });

  it("recuerda el ancho elegido", async () => {
    mockApi({});
    renderApp();
    await screen.findByLabelText(/ancho/i);
    fireEvent.keyDown(handle(), { key: "ArrowLeft" });
    cleanup();

    mockApi({});
    renderApp();
    await screen.findByLabelText(/ancho/i);

    expect(handle().getAttribute("aria-valuenow")).toBe("384");
  });

  it("un valor guardado fuera de rango se corrige", async () => {
    localStorage.setItem("dips.rail-width", "5000");
    mockApi({});
    renderApp();
    await screen.findByLabelText(/ancho/i);

    expect(handle().getAttribute("aria-valuenow")).toBe("600");
  });

  it("doble clic vuelve al ancho predeterminado", async () => {
    mockApi({});
    renderApp();
    await screen.findByLabelText(/ancho/i);
    fireEvent.keyDown(handle(), { key: "End" });

    fireEvent.doubleClick(handle());

    expect(handle().getAttribute("aria-valuenow")).toBe("400");
  });
});

describe("textos de la interfaz", () => {
  it("ningún texto de la interfaz es editable", async () => {
    mockApi({});
    renderApp();

    await screen.findByRole("heading", { name: "Carpetas" });
    expect(document.querySelector("[contenteditable]")).toBeNull();
  });
});

