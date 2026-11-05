import type { OutputLimits, OutputOptions } from "./api";

export type OutputDraft = { width: string; height: string; background: string };

export const BACKGROUND_LABELS: Record<string, string> = {
  white: "Blanco",
  transparent: "Transparente",
};

export const draftFrom = (limits: OutputLimits): OutputDraft => ({
  width: String(limits.width),
  height: String(limits.height),
  background: limits.background,
});

/** El problema de lo ingresado, con los mismos límites que valida el motor. */
export function outputProblem(draft: OutputDraft, limits: OutputLimits): string | null {
  for (const [label, value] of [
    ["ancho", draft.width],
    ["alto", draft.height],
  ]) {
    if (!/^\d+$/.test(value)) return `El ${label} debe ser un número entero.`;
    const side = Number(value);
    if (side < limits.min_side || side > limits.max_side) {
      return `El ${label} debe estar entre ${limits.min_side} y ${limits.max_side} píxeles.`;
    }
  }

  if (Number(draft.width) * Number(draft.height) > limits.max_pixels) {
    return `La imagen final no puede superar ${limits.max_pixels.toLocaleString("es-ES")} píxeles.`;
  }

  return null;
}

export const optionsFrom = (draft: OutputDraft): OutputOptions => ({
  width: Number(draft.width),
  height: Number(draft.height),
  background: draft.background,
});

export function describeOptions(options: OutputOptions): string {
  const background = (BACKGROUND_LABELS[options.background] ?? options.background).toLowerCase();
  return `${options.width} × ${options.height} px, fondo ${background}`;
}

/** Un lienzo de muestra con la proporción y el fondo elegidos. */
function CanvasSample({ draft, valid }: { draft: OutputDraft; valid: boolean }) {
  const ratio = valid ? `${draft.width} / ${draft.height}` : "1 / 1";

  return (
    <div className="canvas-sample" aria-hidden="true">
      <div className={`canvas canvas-${draft.background}`} style={{ aspectRatio: ratio }}>
        <span className="canvas-product" />
      </div>
      <span className="canvas-size">{valid ? `${draft.width} × ${draft.height}` : "—"}</span>
    </div>
  );
}

export default function OutputForm({
  draft,
  limits,
  disabled,
  problem,
  onChange,
}: {
  draft: OutputDraft;
  limits: OutputLimits;
  disabled: boolean;
  problem: string | null;
  onChange: (draft: OutputDraft) => void;
}) {
  return (
    <div className="output">
      <div className="output-controls">
        <div className="sizes">
          <label className="field">
            Ancho (px)
            <input
              type="number"
              inputMode="numeric"
              min={limits.min_side}
              max={limits.max_side}
              step={1}
              value={draft.width}
              disabled={disabled}
              onChange={(event) => onChange({ ...draft, width: event.target.value })}
            />
          </label>
          <span className="times" aria-hidden="true">
            ×
          </span>
          <label className="field">
            Alto (px)
            <input
              type="number"
              inputMode="numeric"
              min={limits.min_side}
              max={limits.max_side}
              step={1}
              value={draft.height}
              disabled={disabled}
              onChange={(event) => onChange({ ...draft, height: event.target.value })}
            />
          </label>
        </div>

        <fieldset className="backgrounds" disabled={disabled}>
          <legend>Fondo</legend>
          {limits.backgrounds.map((background) => (
            <label className="swatch-option" key={background}>
              <input
                type="radio"
                name="background"
                value={background}
                checked={draft.background === background}
                disabled={disabled}
                onChange={() => onChange({ ...draft, background })}
              />
              <span className={`swatch canvas-${background}`} aria-hidden="true" />
              {BACKGROUND_LABELS[background] ?? background}
            </label>
          ))}
        </fieldset>

      </div>

      <CanvasSample draft={draft} valid={problem === null} />

      {problem && <p className="field-error">{problem}</p>}
      <p className="hint">
        Si cambia el tamaño o el fondo, se crean resultados nuevos con otro nombre; los anteriores
        no se modifican.
      </p>
    </div>
  );
}
