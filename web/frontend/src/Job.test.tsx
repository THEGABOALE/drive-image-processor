import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import type { Job, JobItem } from "./api";

const ACCOUNT = { sub: "sub-a", email: "prueba@example.com", display_name: "Prueba" };
const CSRF = "token-csrf-de-prueba";
const JOB_ID = "a".repeat(32);

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

async function selectAndVerify() {
  fireEvent.click(await screen.findByRole("button", { name: /elegir carpeta de origen/i }));
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

    fireEvent.click(await screen.findByRole("button", { name: /elegir carpeta de origen/i }));
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
