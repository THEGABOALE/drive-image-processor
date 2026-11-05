import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  cancelConnection,
  disconnect,
  getCurrentJob,
  getStatus,
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
import JobPanel from "./Job";
import OutputForm, { draftFrom, optionsFrom, outputProblem, type OutputDraft } from "./Output";
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
  const [output, setOutput] = useState<OutputDraft | null>(null);
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

  // El trabajo sigue en el backend aunque se cierre o recargue la página.
  useEffect(() => {
    if (connectedSub === null) return;
    getCurrentJob()
      .then(setJob)
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
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connectedSub, pickerConfig?.client_id]);

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
        setJob(null);
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
    setJob(null);
  };

  const preview = () =>
    run(async () => {
      const targetId = sameFolder ? source?.id : target?.id;
      if (!source || !targetId || !output || problem_) return;
      setJob(await startPreview(csrf, source.id, targetId, optionsFrom(output)));
    });

  const changeOutput = (draft: OutputDraft) => {
    setOutput(draft);
    setJob(null);
  };

  const execute = () =>
    run(async () => {
      if (job) setJob(await startRun(csrf, job.id));
    });

  const ready = Boolean(source && (sameFolder || target));
  const connectionAlert = connection?.error ? connection.message : null;

  const connected = connection?.state === "connected";
  const accountName = connection?.account?.display_name || connection?.account?.email || "";

  return (
    <div className="page">
      <header className="topbar">
        <div className="brand">
          <span className="mark" aria-hidden="true" />
          <span>Drive Image Processor</span>
        </div>
        {status?.environment === "development" && (
          <span className="badge">Entorno de desarrollo</span>
        )}
      </header>

      <section className="hero">
        <h1>
          Quite el fondo de sus fotos de producto <span className="highlight">en Drive</span>
        </h1>
        <p className="lead">
          Elija una carpeta de Google Drive: cada foto se recorta, se centra en un lienzo blanco o
          transparente y se guarda como PNG en la carpeta que usted indique. Las fotos originales
          nunca se tocan.
        </p>
      </section>

      {(problem || connectionAlert) && (
        <p className="alert" role="alert">
          {problem ?? connectionAlert}
        </p>
      )}

      <main className="steps">
        <Step number={1} title="Cuenta de Google Drive" done={connected}>
          {!status && !problem && <p className="hint">Consultando el estado…</p>}

          {connected && (
            <div className="account">
              <span className="avatar" aria-hidden="true">
                {(accountName || "?").charAt(0).toUpperCase()}
              </span>
              <div className="account-text">
                <p className="account-name">
                  {connection.account?.email ?? "Cuenta conectada"}
                </p>
                {connection.account?.display_name && (
                  <p className="hint">{connection.account.display_name}</p>
                )}
              </div>
              <button className="quiet" onClick={leave} disabled={busy || jobRunning}>
                Desconectar
              </button>
            </div>
          )}

          {authorizing && (
            <div className="account">
              <span className="spinner" aria-hidden="true" />
              <p className="account-text">Complete la autorización en el navegador ({authorizing}).</p>
              <button className="quiet" onClick={cancel} disabled={busy}>
                Cancelar
              </button>
            </div>
          )}

          {connection?.state === "disconnected" && (
            <div className="connect">
              <button className="primary large" onClick={connect} disabled={busy}>
                Conectar Google Drive
              </button>
              <p className="hint">
                Google pedirá dos autorizaciones: una para leer los originales y otra para crear los
                resultados. Use la misma cuenta en ambas.
              </p>
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
        </Step>

        <Step
          number={2}
          title="Carpetas"
          done={Boolean(verification)}
          locked={!connected}
          lockedText="Primero conecte su cuenta de Google."
        >
          <label className="check">
            <input
              type="checkbox"
              checked={sameFolder}
              disabled={jobRunning}
              onChange={(event) => toggleSameFolder(event.target.checked)}
            />
            Usar la misma carpeta como origen y destino
          </label>

          <div className="route">
            <FolderSlot
              label="Carpeta de origen"
              action="origen"
              folder={source}
              busy={busy || !pickerReady || jobRunning}
              onChoose={() => choose("Elegir la carpeta de origen", setSource)}
            />
            {!sameFolder && (
              <>
                <span className="route-arrow" aria-hidden="true">
                  →
                </span>
                <FolderSlot
                  label="Carpeta de destino"
                  action="destino"
                  folder={target}
                  busy={busy || !pickerReady || jobRunning}
                  onChoose={() => choose("Elegir la carpeta de destino", setTarget)}
                />
              </>
            )}
          </div>

          <div className="actions">
            <button
              className={verification ? "quiet" : "primary"}
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
          </div>
        </Step>

        <Step
          number={3}
          title="Resultado"
          done={Boolean(verification) && Boolean(output) && !problem_}
          locked={!connected || !limits || !output}
          lockedText="Elija el tamaño y el fondo cuando la cuenta esté conectada."
        >
          {limits && output && (
            <OutputForm
              draft={output}
              limits={limits}
              disabled={jobRunning}
              problem={problem_}
              onChange={changeOutput}
            />
          )}
        </Step>

        <Step
          number={4}
          title="Vista previa y procesamiento"
          done={job?.kind === "run" && job.status === "finished" && Boolean(job.summary?.complete)}
          locked={!connected || (!verification && !job)}
          lockedText="Verifique las carpetas para ver qué se va a procesar."
        >
          {verification && !jobRunning && (
            <div className="actions">
              <button
                className={job ? "quiet" : "primary large"}
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

          {job && <JobPanel job={job} busy={busy} onRun={execute} />}
        </Step>
      </main>

      <footer className="footer">
        La aplicación funciona solo en este equipo. Para salir, cierre la ventana de la aplicación.
      </footer>
    </div>
  );
}

function Step({
  number,
  title,
  done = false,
  locked = false,
  lockedText,
  children,
}: {
  number: number;
  title: string;
  done?: boolean;
  locked?: boolean;
  lockedText?: string;
  children: React.ReactNode;
}) {
  const id = `step-${number}`;
  const state = locked ? "locked" : done ? "done" : "open";

  return (
    <section className={`step ${state}`} aria-labelledby={id}>
      <header className="step-head">
        <span className="step-number" aria-hidden="true">
          {done && !locked ? "✓" : number}
        </span>
        <h2 id={id}>{title}</h2>
      </header>
      {locked ? <p className="hint">{lockedText}</p> : <div className="step-body">{children}</div>}
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
      <h3>{label}</h3>
      <p className={folder ? "folder-name" : "hint"}>{folder ? folder.name : "Sin elegir"}</p>
      <button className="quiet" onClick={onChoose} disabled={busy}>
        {folder ? `Cambiar ${action}` : `Elegir carpeta de ${action}`}
      </button>
    </div>
  );
}
