import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  cancelConnection,
  disconnect,
  getCurrentJob,
  getStatus,
  regenerateRemoved,
  startConnection,
  startPreview,
  startRun,
  verifyFolders,
  type Connection,
  type Job,
  type Status,
  type Verification,
} from "./api";
import {
  GooglePicker,
  loadGoogleApis,
  type PickedFolder,
  type PickerConfig,
} from "./google/picker";
import JobPanel, { LastJob, nameLength } from "./Job";
import SampleCanvas from "./Sample";
import OutputForm, { draftFrom, optionsFrom, outputProblem, type OutputDraft } from "./Output";
import exampleOriginal from "./assets/ejemplo-original.webp";
import exampleResult from "./assets/ejemplo-resultado.webp";
import "./styles.css";

type PickerLike = Pick<GooglePicker, "pick" | "forget">;

type AppProps = {
  createPicker?: (config: PickerConfig, expectedSub: string) => PickerLike;
  loadApis?: () => Promise<void>;
  pollMs?: number;
};

const AUTHORIZING_STEP: Partial<Record<Connection["state"], string>> = {
  authorizing_read: "paso 1 de 2: leer los originales",
  authorizing_write: "paso 2 de 2: crear los resultados",
};

const message = (error: unknown) =>
  error instanceof Error ? error.message : "Error inesperado.";

export default function App({
  createPicker = (config, sub) => new GooglePicker(config, sub),
  loadApis = loadGoogleApis,
  pollMs = 1500,
}: AppProps) {
  const [status, setStatus] = useState<Status | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [source, setSource] = useState<PickedFolder | null>(null);
  const [target, setTarget] = useState<PickedFolder | null>(null);
  const [sameFolder, setSameFolder] = useState(false);
  const [verification, setVerification] = useState<Verification | null>(null);
  const [busy, setBusy] = useState(false);
  const [pickerReady, setPickerReady] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  // Un trabajo terminado no se abre solo: queda a mano en el inicio.
  const [lastJob, setLastJob] = useState<Job | null>(null);
  const [output, setOutput] = useState<OutputDraft | null>(null);
  const [railWidth, setRailWidth] = useState(storedRailWidth);
  const picker = useRef<PickerLike | null>(null);
  const apis = useRef<Promise<void> | null>(null);

  const connection = status?.connection;
  const csrf = status?.csrf_token ?? "";
  const pickerConfig = status?.picker;
  const connectedSub =
    connection?.state === "connected" ? (connection.account?.sub ?? "") : null;
  const authorizing = connection ? AUTHORIZING_STEP[connection.state] : undefined;
  const jobRunning = job?.status === "running";
  const limits = status?.output;
  const problem_ = output && limits ? outputProblem(output, limits) : null;

  // Los valores por defecto los informa el backend (los del motor).
  useEffect(() => {
    if (limits && output === null) setOutput(draftFrom(limits));
  }, [limits, output]);

  const refresh = useCallback(async () => {
    setStatus(await getStatus());
  }, []);

  useEffect(() => {
    refresh().catch((error) => setProblem(message(error)));
  }, [refresh]);

  useEffect(() => {
    if (!authorizing) return;
    const timer = setInterval(() => {
      refresh().catch(() => {});
    }, pollMs);
    return () => clearInterval(timer);
  }, [authorizing, refresh, pollMs]);

  // El trabajo sigue en el backend aunque se cierre o recargue la página. Si
  // sigue en curso se muestra; si terminó, se ofrece sin ocupar el inicio.
  useEffect(() => {
    if (connectedSub === null) return;
    getCurrentJob()
      .then((current) => {
        if (current?.status === "running") setJob(current);
        else setLastJob(current);
      })
      .catch((error) => setProblem(message(error)));
  }, [connectedSub]);

  useEffect(() => {
    if (!jobRunning) return;
    const timer = setInterval(() => {
      Promise.all([getCurrentJob(), refresh()])
        .then(([current]) => setJob(current))
        .catch(() => {});
    }, pollMs);
    return () => clearInterval(timer);
  }, [jobRunning, refresh, pollMs]);

  // El Picker y lo elegido pertenecen a la cuenta conectada.
  useEffect(() => {
    if (connectedSub === null || !pickerConfig) return;

    const current = createPicker(pickerConfig, connectedSub);
    picker.current = current;
    apis.current ??= loadApis();
    apis.current
      .then(() => setPickerReady(true))
      .catch((error) => setProblem(message(error)));

    return () => {
      current.forget();
      picker.current = null;
      setSource(null);
      setTarget(null);
      setSameFolder(false);
      setVerification(null);
      setJob(null);
      setLastJob(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectedSub, pickerConfig?.client_id]);

  // Cerrar el trabajo mostrado lo deja como el último, para volver a abrirlo.
  const closeJob = () => {
    if (job) setLastJob(job);
    setJob(null);
  };

  const openLastJob = () => setJob(lastJob);

  const applyConnection = (next: Connection) =>
    setStatus((previous) => previous && { ...previous, connection: next });

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setProblem(null);
    try {
      await action();
    } catch (error) {
      setProblem(message(error));
    } finally {
      setBusy(false);
    }
  }

  const connect = () =>
    run(async () => applyConnection(await startConnection(csrf)));
  const cancel = () =>
    run(async () => applyConnection(await cancelConnection(csrf)));
  const leave = () => run(async () => applyConnection(await disconnect(csrf)));

  // Sin nada antes de pick(): el navegador solo permite la ventana de
  // autorización de Google como respuesta directa al clic.
  const choose = (title: string, assign: (folder: PickedFolder) => void) =>
    run(async () => {
      const folder = await picker.current?.pick(title);
      if (folder) {
        setVerification(null);
        closeJob();
        assign(folder);
      }
    });

  const verify = () =>
    run(async () => {
      const targetId = sameFolder ? source?.id : target?.id;
      if (!source || !targetId) return;
      setVerification(null);
      try {
        setVerification(await verifyFolders(csrf, source.id, targetId));
      } catch (error) {
        if (
          error instanceof ApiError &&
          (error.code === "reconnect_required" || error.code === "not_connected")
        ) {
          await refresh();
        }
        throw error;
      }
    });

  const toggleSameFolder = (checked: boolean) => {
    setSameFolder(checked);
    setVerification(null);
    closeJob();
  };

  const preview = () =>
    run(async () => {
      const targetId = sameFolder ? source?.id : target?.id;
      if (!source || !targetId || !output || problem_) return;
      setJob(await startPreview(csrf, source.id, targetId, optionsFrom(output)));
    });

  const changeOutput = (draft: OutputDraft) => {
    setOutput(draft);
    closeJob();
  };

  const regenerate = () =>
    run(async () => {
      if (job) setJob(await regenerateRemoved(csrf, job.id));
    });

  const execute = () =>
    run(async () => {
      if (job) setJob(await startRun(csrf, job.id));
    });

  const ready = Boolean(source && (sameFolder || target));
  const connectionAlert = connection?.error ? connection.message : null;

  const connected = connection?.state === "connected";
  const accountName = connection?.account?.display_name || connection?.account?.email || "";
  // La etiqueta es para quien desarrolla; el ejecutable se entrega a otras personas.
  const development = status?.environment === "development" && !status.packaged;
  const alert = (problem || connectionAlert) && (
    <p className="alert" role="alert">
      {problem ?? connectionAlert}
    </p>
  );

  if (!connected) {
    return (
      <div className="welcome">
        <header className="welcome-top">
          <Brand />
          {development && <DevBadge />}
        </header>

        <main className="welcome-body">
          <section className="welcome-copy" aria-labelledby="account-title">
            <h1>Fotos de producto sin fondo, directo en su Drive</h1>
            <div className="welcome-panel">
              <p className="lead">
                La aplicación toma cada foto de una carpeta, recorta el producto, lo centra en un
                lienzo blanco o transparente y guarda el PNG en la carpeta que usted elija. Las fotos
                originales nunca se tocan.
              </p>

              <ol className="how">
                <li>Conecte su cuenta de Google.</li>
                <li>Elija la carpeta de las fotos y dónde guardar los resultados.</li>
                <li>Revise la vista previa y confirme el procesamiento.</li>
              </ol>

              <h2 id="account-title" className="visually-hidden">
                Cuenta de Google Drive
              </h2>

              {alert}

              {!status && !problem && <p className="hint">Consultando el estado…</p>}

              {connection?.state === "disconnected" && (
                <div className="connect">
                  <button className="primary large" onClick={connect} disabled={busy}>
                    Conectar Google Drive
                  </button>
                  <p className="hint">
                    Google pedirá dos autorizaciones: una para leer los originales y otra para crear
                    los resultados. Use la misma cuenta en ambas.
                  </p>
                </div>
              )}

              {authorizing && (
                <div className="connect waiting">
                  <span className="spinner" aria-hidden="true" />
                  <p>Complete la autorización en el navegador ({authorizing}).</p>
                  <button onClick={cancel} disabled={busy}>
                    Cancelar
                  </button>
                </div>
              )}

              {connection?.state === "reconnect_required" && (
                <div className="connect">
                  <p>{connection.message ?? "La autorización ya no es válida."}</p>
                  <button className="primary large" onClick={connect} disabled={busy}>
                    Volver a conectar
                  </button>
                </div>
              )}
            </div>
          </section>

          <Comparison />
        </main>
      </div>
    );
  }

  const canPreview = Boolean(verification) && !jobRunning;
  // Con una foto que mirar, en pantallas angostas va antes que los controles.
  const shellClass = job || verification ? "shell has-work" : "shell";

  return (
    <div className={shellClass} style={{ ["--rail-width" as string]: `${railWidth}px` }}>
      <aside className="rail">
        <header className="rail-top">
          <Brand />
        </header>

        <Section title="Cuenta de Google Drive" done compact>
          <div className="account">
            <span className="avatar" aria-hidden="true">
              {(accountName || "?").charAt(0).toUpperCase()}
            </span>
            <div className="account-text">
              <p className="account-name">{connection.account?.email ?? "Cuenta conectada"}</p>
              {connection.account?.display_name && (
                <p className="hint">{connection.account.display_name}</p>
              )}
            </div>
            <button className="small" onClick={leave} disabled={busy || jobRunning}>
              Desconectar
            </button>
          </div>
        </Section>

        <Section number={1} title="Carpetas" done={Boolean(verification)}>
          <label className="check">
            <input
              type="checkbox"
              checked={sameFolder}
              disabled={jobRunning}
              onChange={(event) => toggleSameFolder(event.target.checked)}
            />
            Usar la misma carpeta como origen y destino
          </label>

          <div className="folders">
            <FolderSlot
              label={sameFolder ? "Carpeta (origen y destino)" : "Carpeta de origen"}
              action={sameFolder ? "origen y destino" : "origen"}
              folder={source}
              busy={busy || !pickerReady || jobRunning}
              onChoose={() =>
                choose(
                  sameFolder ? "Elegir la carpeta de origen y destino" : "Elegir la carpeta de origen",
                  setSource,
                )
              }
            />
            {!sameFolder && (
              <FolderSlot
                label="Carpeta de destino"
                action="destino"
                folder={target}
                busy={busy || !pickerReady || jobRunning}
                onChoose={() => choose("Elegir la carpeta de destino", setTarget)}
              />
            )}
          </div>

          <button
            className={verification ? "" : "primary"}
            onClick={verify}
            disabled={busy || !ready || jobRunning}
          >
            Verificar carpetas
          </button>
          {verification && (
            <p className="success" role="status">
              {verification.same_folder
                ? "Verificada: misma carpeta para originales y resultados."
                : "Carpetas verificadas: se pueden leer los originales y crear los resultados."}
            </p>
          )}
        </Section>

        <Section
          number={2}
          title="Resultado"
          done={Boolean(verification) && Boolean(output) && !problem_}
          locked={!verification}
        >
          {!verification && (
            <p className="hint">Disponible después de verificar las carpetas.</p>
          )}
          {limits && output && (
            <OutputForm
              draft={output}
              limits={limits}
              disabled={jobRunning || !verification}
              problem={problem_}
              onChange={changeOutput}
            />
          )}
        </Section>

        <div className="rail-end">
          {canPreview && (
            <div className="rail-action">
              {/* Con un trabajo a la vista, su acción es la amarilla: esta se aparta. */}
              <button
                className={job ? "large" : "primary large"}
                onClick={preview}
                disabled={busy || Boolean(problem_) || !output}
              >
                Ver vista previa
              </button>
              {!job && (
                <p className="hint">Nada se modifica hasta que confirme el procesamiento.</p>
              )}
            </div>
          )}
          {development && (
            <footer className="rail-foot">
              <DevBadge />
            </footer>
          )}
        </div>
      </aside>

      <RailHandle width={railWidth} onChange={setRailWidth} />

      <main className="stage" aria-label="Vista previa y procesamiento">
        {alert}
        {job ? (
          <JobPanel
            job={job}
            busy={busy}
            onRun={execute}
            onClose={closeJob}
            onRegenerate={regenerate}
          />
        ) : verification ? (
          <div
            className="stage-ready"
            style={
              output && !problem_
                ? { ["--ratio" as string]: String(Number(output.width) / Number(output.height)) }
                : undefined
            }
          >
            {output && !problem_ && <SampleCanvas options={optionsFrom(output)} />}
            <div className="stage-side">
              <div className="stage-note">
                <h2>Todo listo para la vista previa</h2>
                <p>
                  Ajuste el tamaño y el fondo: este es el lienzo que va a recibir cada foto. La
                  vista previa muestra qué se va a procesar, sin cambiar nada todavía.
                </p>
              </div>
              {lastJob && <LastJob job={lastJob} onOpen={openLastJob} />}
            </div>
          </div>
        ) : (
          <div className="stage-start">
            <FolderRoute source={source} target={target} sameFolder={sameFolder} />
            <div className="stage-side">
              <div className="stage-note">
                <h2>Empiece eligiendo las carpetas</h2>
                <p>
                  {ready
                    ? "Ya están elegidas. Verifíquelas para ajustar el resultado y ver la vista previa."
                    : "Elija la carpeta con las fotos y dónde guardar los resultados. Después podrá ajustar el tamaño y el fondo, y revisar todo antes de procesar."}
                </p>
              </div>
              {lastJob && <LastJob job={lastJob} onOpen={openLastJob} />}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

function Brand() {
  return (
    <div className="brand">
      <span className="brand-acronym">DIPS</span>
      <span className="brand-name">Drive Image Processing Service</span>
    </div>
  );
}

function DevBadge() {
  return <span className="badge">Entorno de desarrollo</span>;
}

const RAIL_MIN = 300;
const RAIL_MAX = 600;
const RAIL_DEFAULT = 400;
const RAIL_STEP = 16;
const RAIL_KEY = "dips.rail-width";

const clampRail = (value: number) => Math.min(RAIL_MAX, Math.max(RAIL_MIN, Math.round(value)));

function storedRailWidth(): number {
  try {
    const saved = Number(localStorage.getItem(RAIL_KEY));
    return saved ? clampRail(saved) : RAIL_DEFAULT;
  } catch {
    return RAIL_DEFAULT;
  }
}

function saveRailWidth(value: number) {
  try {
    localStorage.setItem(RAIL_KEY, String(value));
  } catch {
    // Sin almacenamiento, el ancho vuelve al predeterminado al recargar.
  }
}

/** El borde del panel: se arrastra con el mouse o se mueve con las flechas. */
function RailHandle({ width, onChange }: { width: number; onChange: (value: number) => void }) {
  const change = (value: number) => {
    const next = clampRail(value);
    onChange(next);
    saveRailWidth(next);
  };

  return (
    <div
      className="rail-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="Redimensionar el panel lateral"
      aria-valuemin={RAIL_MIN}
      aria-valuemax={RAIL_MAX}
      aria-valuenow={width}
      tabIndex={0}
      onPointerDown={(event) => {
        const start = event.clientX;
        const initial = width;
        const target = event.currentTarget;
        target.setPointerCapture?.(event.pointerId);
        document.body.classList.add("resizing");

        const move = (moveEvent: PointerEvent) => change(initial + moveEvent.clientX - start);
        const stop = () => {
          document.body.classList.remove("resizing");
          target.removeEventListener("pointermove", move);
          target.removeEventListener("pointerup", stop);
          target.removeEventListener("pointercancel", stop);
        };
        target.addEventListener("pointermove", move);
        target.addEventListener("pointerup", stop);
        target.addEventListener("pointercancel", stop);
      }}
      onDoubleClick={() => change(RAIL_DEFAULT)}
      onKeyDown={(event) => {
        const keys: Record<string, number> = {
          ArrowLeft: width - RAIL_STEP,
          ArrowRight: width + RAIL_STEP,
          Home: RAIL_MIN,
          End: RAIL_MAX,
        };
        if (event.key in keys) {
          event.preventDefault();
          change(keys[event.key]);
        }
      }}
    />
  );
}

/**
 * Antes y después: una foto real y el recorte que hizo el propio DIPS, sin
 * retocar. Las dos imágenes tienen el mismo tamaño, así que calzan al cortar.
 */
function Comparison() {
  return (
    <figure className="comparison" aria-label="Ejemplo: la misma foto con su fondo y sin él">
      <div className="comparison-scene comparison-before" aria-hidden="true">
        <img src={exampleOriginal} alt="" width={736} height={736} decoding="async" />
      </div>
      <div className="comparison-scene comparison-after" aria-hidden="true">
        <img src={exampleResult} alt="" width={736} height={736} decoding="async" />
      </div>
      <span className="comparison-handle" aria-hidden="true" />
      <figcaption>
        <span>Original</span>
        <span>Resultado</span>
      </figcaption>
    </figure>
  );
}

function Section({
  number,
  title,
  done = false,
  compact = false,
  locked = false,
  children,
}: {
  number?: number;
  title: string;
  done?: boolean;
  compact?: boolean;
  locked?: boolean;
  children: React.ReactNode;
}) {
  const id = `section-${title.replace(/\s+/g, "-").toLowerCase()}`;
  const classes = ["rail-section", done && "done", compact && "compact", locked && "locked"];

  return (
    <section
      className={classes.filter(Boolean).join(" ")}
      aria-labelledby={id}
    >
      <h2 id={id} className={compact ? "visually-hidden" : undefined}>
        {number !== undefined && (
          <span className="step-number" aria-hidden="true">
            {done ? "✓" : number}
          </span>
        )}
        {title}
      </h2>
      {children}
    </section>
  );
}

function FolderSlot({
  label,
  action,
  folder,
  busy,
  onChoose,
}: {
  label: string;
  action: string;
  folder: PickedFolder | null;
  busy: boolean;
  onChoose: () => void;
}) {
  return (
    <div className={folder ? "slot chosen" : "slot"}>
      <span className="folder-icon" aria-hidden="true" />
      <div className="slot-text">
        <h3>{label}</h3>
        <p className={folder ? "folder-name" : "hint"}>{folder ? folder.name : "Sin elegir"}</p>
      </div>
      <button className="small" onClick={onChoose} disabled={busy}>
        {folder ? `Cambiar ${action}` : `Elegir carpeta de ${action}`}
      </button>
    </div>
  );
}

/** De dónde salen las fotos y adónde llegan los resultados. */
function FolderRoute({
  source,
  target,
  sameFolder,
}: {
  source: PickedFolder | null;
  target: PickedFolder | null;
  sameFolder: boolean;
}) {
  return (
    <figure className={sameFolder ? "route single" : "route"} aria-label="Recorrido de las fotos">
      {sameFolder ? (
        <RouteStop
          kind="both"
          label="Carpeta de origen y destino"
          detail="Las fotos y sus PNG sin fondo quedan juntos"
          folder={source}
        />
      ) : (
        <>
          <RouteStop
            kind="source"
            label="Carpeta de origen"
            detail="Las fotos originales, sin cambios"
            folder={source}
          />
          <span className="route-link" aria-hidden="true">
            <span className="route-step">Quitar fondo y centrar</span>
          </span>
          <RouteStop
            kind="target"
            label="Carpeta de destino"
            detail="Los PNG sin fondo"
            folder={target}
          />
        </>
      )}
    </figure>
  );
}

function RouteStop({
  kind,
  label,
  detail,
  folder,
}: {
  kind: "source" | "target" | "both";
  label: string;
  detail: string;
  folder: PickedFolder | null;
}) {
  return (
    <div className={`route-stop route-${kind}${folder ? " chosen" : ""}`}>
      <span className="route-caption">
        <span className="route-label">{label}</span>
        <span className="route-detail">{detail}</span>
      </span>
      <span className={["route-name", folder ? nameLength(folder.name) : "empty"].join(" ")}>
        {folder ? folder.name : "Sin elegir"}
      </span>
    </div>
  );
}
