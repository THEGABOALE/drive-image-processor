import type { Job, JobItem } from "./api";

const STAGES = [
  { key: "download", label: "Descarga" },
  { key: "processing", label: "Procesamiento" },
  { key: "upload", label: "Subida" },
] as const;

const STAGE_NAMES: Record<string, string> = {
  download: "la descarga",
  processing: "el procesamiento",
  upload: "la subida",
};

// En la vista previa, "complete" es lo hecho antes; en el resumen de una
// ejecución incluye lo que se acaba de subir.
const STATUS_LABELS: Record<JobItem["status"], string> = {
  complete: "En Drive",
  planned: "Se procesa",
  failed: "Con error",
  pending: "Pendiente de confirmar",
  not_started: "No iniciada",
};

const RELATED_LABELS: Record<string, string> = {
  previous_revision: "de una versión anterior",
  other_configuration: "de otra configuración",
  other_copy: "otra copia del mismo resultado",
};

const imageCount = (count: number) =>
  count === 1 ? "1 imagen" : `${count} imágenes`;

function ItemRow({ item, isPreview }: { item: JobItem; isPreview: boolean }) {
  const bad = item.status === "failed" || item.status === "pending";
  const label =
    isPreview && item.status === "complete" ? "Ya está en Drive" : STATUS_LABELS[item.status];

  return (
    <li>
      <div className="item-head">
        <span className="item-name">{item.name}</span>
        <span className={bad ? "item-status bad" : "item-status"}>
          {label}
          {bad && item.stage ? ` en ${STAGE_NAMES[item.stage]}` : ""}
        </span>
      </div>
      {item.reason && <p className="item-detail">{item.reason}</p>}
      {item.notes.map((note) => (
        <p className="item-detail" key={note}>
          {note}
        </p>
      ))}
      {item.related.map((related) => (
        <p className="item-detail" key={related.name}>
          En Drive hay un PNG {RELATED_LABELS[related.kind] ?? "relacionado"}: {related.name}.
          No se modifica.
        </p>
      ))}
    </li>
  );
}

function Progress({ job }: { job: Job }) {
  const { progress } = job;
  const { total } = progress;
  const waitingForModel =
    job.kind === "run" &&
    total !== null &&
    total > 0 &&
    progress.download.done >= total &&
    progress.processing.done === 0;

  return (
    <>
      <ul className="progress" aria-label="Progreso">
        {STAGES.map(({ key, label }) => {
          const { done, failed } = progress[key];
          return (
            <li key={key} className={progress.stage === key ? "active" : undefined}>
              <span>{label}</span>
              <span>
                {total === null ? done : `${done} de ${total}`}
                {failed > 0 ? ` (${failed} con errores)` : ""}
              </span>
            </li>
          );
        })}
      </ul>
      {progress.current && <p className="muted">Última imagen: {progress.current}</p>}
      {waitingForModel && (
        <p className="muted">
          Preparando el modelo de IA. La primera vez se descarga y puede tardar varios minutos.
        </p>
      )}
      <p className="muted">
        Puede cerrar esta pestaña: el trabajo sigue mientras la aplicación esté abierta.
      </p>
    </>
  );
}

function Summary({ job, busy, onRun }: { job: Job; busy: boolean; onRun: () => void }) {
  const summary = job.summary!;
  const { counts } = summary;
  const toRun = counts.planned + counts.pending;
  const isPreview = job.kind === "preview";
  const unfinished = !isPreview && !summary.complete;

  return (
    <>
      <ul className="counts">
        {isPreview ? (
          <>
            <li>{counts.planned} por procesar</li>
            <li>{counts.complete} ya en Drive</li>
          </>
        ) : (
          <>
            <li>
              {summary.uploaded_now === 1
                ? "1 PNG subido ahora"
                : `${summary.uploaded_now} PNG subidos ahora`}
            </li>
            <li>{counts.complete} en Drive</li>
          </>
        )}
        {counts.failed > 0 && <li>{counts.failed} con errores</li>}
        {counts.pending > 0 && (
          <li>
            {counts.pending === 1
              ? "1 pendiente de confirmar"
              : `${counts.pending} pendientes de confirmar`}
          </li>
        )}
        {counts.not_started > 0 && (
          <li>
            {counts.not_started === 1 ? "1 no iniciada" : `${counts.not_started} no iniciadas`}
          </li>
        )}
      </ul>

      {summary.stopped && (
        <p className="alert" role="alert">
          Se detuvo en {STAGE_NAMES[summary.stopped.stage] ?? "una etapa"}: {summary.stopped.message}
        </p>
      )}

      {summary.items.length > 0 && (
        <ul className="items">
          {summary.items.map((item) => (
            <ItemRow item={item} isPreview={isPreview} key={item.file_id} />
          ))}
        </ul>
      )}

      {summary.warnings.map((warning) => (
        <p className="item-detail" key={warning}>
          {warning}
        </p>
      ))}

      {unfinished && (
        <p>Quedó trabajo sin terminar. Vuelva a generar la vista previa: lo ya hecho no se repite.</p>
      )}

      {isPreview && (
        <p className="muted">
          Los originales no se modifican ni se borran: se crean PNG nuevos en el destino. Es una
          estimación: lo que dependa de la descarga o del procesamiento se sabrá al ejecutar. Si
          agrega fotos a la carpeta antes de ejecutar, también se procesan.
        </p>
      )}

      {isPreview && !summary.can_run && !summary.stopped && (
        <p>No hay nada que procesar.</p>
      )}

      {summary.can_run && (
        <button onClick={onRun} disabled={busy}>
          Procesar {imageCount(toRun)}
        </button>
      )}
    </>
  );
}

export default function JobPanel({
  job,
  busy,
  onRun,
}: {
  job: Job;
  busy: boolean;
  onRun: () => void;
}) {
  const title = job.kind === "preview" ? "Vista previa" : "Procesamiento";

  return (
    <section className="panel" aria-labelledby="job-title">
      <h2 id="job-title">{title}</h2>

      {job.source.name && (
        <p className="job-folders">
          {job.same_folder
            ? `Carpeta: ${job.source.name}`
            : `De ${job.source.name} a ${job.target.name}`}
        </p>
      )}

      {job.status === "running" && <Progress job={job} />}
      {job.status === "finished" && job.summary && (
        <Summary job={job} busy={busy} onRun={onRun} />
      )}
      {job.error && (
        <p className="alert" role="alert">
          {job.error.message}
        </p>
      )}
    </section>
  );
}
