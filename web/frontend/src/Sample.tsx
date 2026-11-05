import { useEffect, useState } from "react";
import type { OutputOptions } from "./api";
import { BACKGROUND_LABELS } from "./Output";

// Espera entre teclas antes de pedir otro ejemplo al servidor.
const DEBOUNCE_MS = 250;

/**
 * Cómo quedaría un resultado con el tamaño y el fondo elegidos. El backend
 * lo dibuja con la misma función del motor, a partir de una foto que la
 * cuenta ya procesó o descargó. Sin fotos todavía, muestra un producto de
 * muestra.
 */
export default function SampleCanvas({ options }: { options: OutputOptions }) {
  const [shown, setShown] = useState(options);
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => setShown(options), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [options.width, options.height, options.background]); // eslint-disable-line react-hooks/exhaustive-deps

  const query = new URLSearchParams({
    width: String(shown.width),
    height: String(shown.height),
    background: shown.background,
  });
  const transparent = shown.background === "transparent";
  const background = (BACKGROUND_LABELS[shown.background] ?? shown.background).toLowerCase();

  return (
    <figure className="sample">
      <div
        className={transparent ? "sample-canvas checker" : "sample-canvas canvas-white"}
        style={{ ["--ratio" as string]: String(shown.width / shown.height) }}
      >
        {missing ? (
          <span className="canvas-product" aria-hidden="true" />
        ) : (
          <img
            src={`/api/sample?${query}`}
            alt={`Ejemplo de resultado de ${shown.width} × ${shown.height} px, fondo ${background}`}
            className={loading ? "loading" : undefined}
            onLoad={() => setLoading(false)}
            onError={() => setMissing(true)}
          />
        )}
      </div>
      <figcaption>
        <span className="sample-size">
          {shown.width} × {shown.height} px
        </span>
        <span>fondo {background}</span>
      </figcaption>
      {missing && (
        <p className="hint">Cuando tenga fotos procesadas, acá verá una de ellas como ejemplo.</p>
      )}
    </figure>
  );
}
