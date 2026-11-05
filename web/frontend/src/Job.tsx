import { useEffect, useRef, useState } from "react";
import type { Job, JobItem } from "./api";
import { describeOptions } from "./Output";

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

// Con más imágenes sin problemas que estas, se agrupan para que los errores
// queden a la vista.
const GROUP_AFTER = 6;

const NEEDS_ATTENTION: JobItem["status"][] = ["failed", "pending", "not_started"];

const imageCount = (count: number) =>
  count === 1 ? "1 imagen" : `${count} imágenes`;

const comparable = (item: JobItem) => Boolean(item.images?.original && item.images?.result);

const prefersReducedMotion = () =>
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** El número sube contando hasta su nuevo valor. */
function useTweened(target: number, duration = 700): number {
  const [value, setValue] = useState(target);
  const from = useRef(target);

  useEffect(() => {
    const start = from.current;
    if (start === target || prefersReducedMotion() || typeof requestAnimationFrame !== "function") {
      from.current = target;
      setValue(target);
      return;
    }

    let frame = 0;
    const began = performance.now();
    const step = (now: number) => {
      const t = Math.min((now - began) / duration, 1);
      // Desaceleración exponencial: llega suave al valor final.
      const eased = t === 1 ? 1 : 1 - 2 ** (-10 * t);
      const current = Math.round(start + (target - start) * eased);
      from.current = current;
      setValue(current);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, duration]);

  return value;
}

/** Original y resultado superpuestos, con una línea que se arrastra. */
export function Compare({ job, item }: { job: Job; item: JobItem }) {
  const [position, setPosition] = useState(50);
  const version = job.finished_at ?? job.created_at;
  const base = `/api/jobs/current/images/${item.file_id}`;
  const ratio = job.options ? job.options.width / job.options.height : 1;
  const transparent = job.options?.background === "transparent";

  return (
    <figure className="compare" style={{ ["--pos" as string]: `${position}%` }}>
      <div className="compare-frame" style={{ ["--ratio" as string]: String(ratio) }}>
        <div className={transparent ? "compare-layer checker" : "compare-layer"}>
          <img src={`${base}/result?v=${version}`} alt={`Resultado de ${item.name}`} />
        </div>
        <div className="compare-layer compare-original">
          <img
            src={`${base}/original?aligned=1&v=${version}`}
            alt={`Original de ${item.name}`}
          />
        </div>
        <span className="compare-line" aria-hidden="true" />
        <span className="compare-tag compare-tag-left" aria-hidden="true">
          Original
        </span>
        <span className="compare-tag compare-tag-right" aria-hidden="true">
          Resultado
        </span>
        <input
          className="compare-range"
          type="range"
          min={0}
          max={100}
          value={position}
          aria-label={`Comparar original y resultado de ${item.name}`}
          onChange={(event) => setPosition(Number(event.target.value))}
        />
      </div>
      <figcaption className="item-name">{item.name}</figcaption>
    </figure>
  );
}

function ItemRow({
  item,
  isPreview,
  selected,
  onCompare,
}: {
  item: JobItem;
  isPreview: boolean;
  selected: boolean;
  onCompare: (item: JobItem) => void;
}) {
  const bad = item.status === "failed" || item.status === "pending";
  const label =
    isPreview && item.status === "complete" ? "Ya está en Drive" : STATUS_LABELS[item.status];
  const classes = ["item", bad ? "bad" : "", selected ? "selected" : ""].filter(Boolean);

  return (
    <li className={classes.join(" ")}>
      <div className="item-head">
        <span className="item-name">{item.name}</span>
        <span className="item-side">
          <span className={`item-status status-${item.status}`}>
            {label}
            {bad && item.stage ? ` en ${STAGE_NAMES[item.stage]}` : ""}
          </span>
          {comparable(item) && (
            <button className="small ghost" aria-pressed={selected} onClick={() => onCompare(item)}>
              Comparar
            </button>
          )}
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

function Items({
  items,
  isPreview,
  selectedId,
  onCompare,
}: {
  items: JobItem[];
  isPreview: boolean;
  selectedId: string | null;
  onCompare: (item: JobItem) => void;
}) {
  const attention = items.filter((item) => NEEDS_ATTENTION.includes(item.status));
  const rest = items.filter((item) => !NEEDS_ATTENTION.includes(item.status));
  const rows = (list: JobItem[]) =>
    list.map((item) => (
      <ItemRow
        item={item}
        isPreview={isPreview}
        selected={item.file_id === selectedId}
        onCompare={onCompare}
        key={item.file_id}
      />
    ));

  return (
    <>
      {attention.length > 0 && <ul className="items">{rows(attention)}</ul>}
      {rest.length > GROUP_AFTER ? (
        <details className="group">
          <summary>Ver las {imageCount(rest.length)} sin problemas</summary>
          <ul className="items">{rows(rest)}</ul>
        </details>
      ) : (
        rest.length > 0 && <ul className="items">{rows(rest)}</ul>
      )}
    </>
  );
}

function overallPercent(job: Job): number | null {
  const { total } = job.progress;
  if (!total) return null;
  const done = STAGES.reduce(
    (sum, { key }) => sum + Math.min(job.progress[key].done, total),
    0,
  );
  return Math.round((done / (STAGES.length * total)) * 100);
}

function Progress({ job }: { job: Job }) {
  const { progress } = job;
  const { total } = progress;
  const percent = overallPercent(job);
  const shown = useTweened(percent ?? 0);
  const waitingForModel =
    job.kind === "run" &&
    total !== null &&
    total > 0 &&
    progress.download.done >= total &&
    progress.processing.done === 0;

  return (
    <>
      <div className="meter">
        {percent !== null && <span className="bar-value">{shown} %</span>}
        <div
          className={percent === null ? "bar indeterminate" : "bar"}
          role="progressbar"
          aria-label="Avance total"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={percent ?? undefined}
        >
          <span style={{ width: percent === null ? undefined : `${percent}%` }} />
        </div>
      </div>

      <ul className="progress" aria-label="Progreso">
        {STAGES.map(({ key, label }) => {
          const { done, failed } = progress[key];
          const finished = total !== null && total > 0 && done >= total;
          const state = progress.stage === key ? "active" : finished ? "finished" : undefined;
          return (
            <li key={key} className={state}>
              <span>{label}</span>
              <span className="numbers">
                {total === null ? done : `${done} de ${total}`}
                {failed > 0 ? ` (${failed} con errores)` : ""}
              </span>
              <span className="track" aria-hidden="true">
                <span style={{ width: total ? `${Math.min(done / total, 1) * 100}%` : "0%" }} />
              </span>
            </li>
          );
        })}
      </ul>
      {progress.current && (
        <p className="hint current" key={progress.current}>
          Última imagen: {progress.current}
        </p>
      )}
      {waitingForModel && (
        <p className="hint">
          Preparando el modelo de IA. La primera vez se descarga y puede tardar varios minutos.
        </p>
      )}
      <p className="hint">
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
  const headline = isPreview
    ? "Así quedaría"
    : summary.complete
      ? "Listo: todo quedó en Drive"
      : "Terminó, pero hay imágenes para revisar";

  const first = summary.items.find(comparable) ?? null;
  const [selectedId, setSelectedId] = useState<string | null>(first?.file_id ?? null);
  const selected = summary.items.find((item) => item.file_id === selectedId && comparable(item));

  return (
    <>
      <p className={unfinished ? "headline warn" : "headline"}>{headline}</p>

      <ul className="counts">
        {isPreview ? (
          <>
            <li className="count accent">{counts.planned} por procesar</li>
            <li className="count">{counts.complete} ya en Drive</li>
          </>
        ) : (
          <>
            <li className="count accent">
              {summary.uploaded_now === 1
                ? "1 PNG subido ahora"
                : `${summary.uploaded_now} PNG subidos ahora`}
            </li>
            <li className="count">{counts.complete} en Drive</li>
          </>
        )}
        {counts.failed > 0 && <li className="count bad">{counts.failed} con errores</li>}
        {counts.pending > 0 && (
          <li className="count bad">
            {counts.pending === 1
              ? "1 pendiente de confirmar"
              : `${counts.pending} pendientes de confirmar`}
          </li>
        )}
        {counts.not_started > 0 && (
          <li className="count bad">
            {counts.not_started === 1 ? "1 no iniciada" : `${counts.not_started} no iniciadas`}
          </li>
        )}
      </ul>

      {summary.stopped && (
        <p className="alert" role="alert">
          Se detuvo en {STAGE_NAMES[summary.stopped.stage] ?? "una etapa"}: {summary.stopped.message}
        </p>
      )}

      {selected && <Compare job={job} item={selected} key={selected.file_id} />}

      {summary.items.length > 0 && (
        <Items
          items={summary.items}
          isPreview={isPreview}
          selectedId={selected?.file_id ?? null}
          onCompare={(item) => setSelectedId(item.file_id)}
        />
      )}

      {summary.warnings.map((warning) => (
        <p className="hint" key={warning}>
          {warning}
        </p>
      ))}

      {unfinished && (
        <p>Quedó trabajo sin terminar. Vuelva a generar la vista previa: lo ya hecho no se repite.</p>
      )}

      {isPreview && (
        <p className="hint">
          Los originales no se modifican ni se borran: se crean PNG nuevos en el destino. Es una
          estimación: lo que dependa de la descarga o del procesamiento se sabrá al ejecutar. Si
          agrega fotos a la carpeta antes de ejecutar, también se procesan.
        </p>
      )}

      {isPreview && !summary.can_run && !summary.stopped && (
        <p className="headline-small">No hay nada que procesar.</p>
      )}

      {summary.can_run && (
        <button className="primary large" onClick={onRun} disabled={busy}>
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
    <div className="job">
      <header className="job-head">
        <h2>{title}</h2>
        <div className="job-meta">
          {job.source.name && (
            <p className="job-folders">
              {job.same_folder
                ? `Carpeta: ${job.source.name}`
                : `De ${job.source.name} a ${job.target.name}`}
            </p>
          )}
          {job.options && <span className="tag">{describeOptions(job.options)}</span>}
        </div>
      </header>

      {job.status === "running" && <Progress job={job} />}
      {job.status === "finished" && job.summary && (
        <Summary job={job} busy={busy} onRun={onRun} key={job.id} />
      )}
      {job.error && (
        <p className="alert" role="alert">
          {job.error.message}
        </p>
      )}
    </div>
  );
}
